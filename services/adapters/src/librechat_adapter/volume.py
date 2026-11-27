"""Ear 私有文件卷中的聊天文件存储（按内容寻址）。

文件字节只写在卷里的 `librechat/blobs/<aa>/<bb>/<sha256>`；`file_ref.path` 是相对卷根的 POSIX
路径，使用方用同一个卷根解析。内容寻址让重放、多条消息引用同一文件、附件被重复导出都只占一份。

写入流程：随机临时名 → 写入并 fsync → 核对大小与 SHA-256 → `os.replace` 到最终名。目录和文件
都以 O_NOFOLLOW/O_EXCL 打开，不跟随符号链接；卷根自身必须是真实目录、不对其他用户可写。
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from pathlib import Path

from ear_contracts import FileRef

SUBDIR = "librechat"
BLOBS = "blobs"
TMP = "tmp"
_STALE_TMP_SECONDS = 24 * 3600


class VolumeError(Exception):
    """私有文件卷不可用或内容与声明不符。固定消息，不含路径和内容。"""


class IntegrityError(VolumeError):
    """收到的字节数或 SHA-256 与导出方声明不一致。"""


def _check_dir(path: Path, mode: int) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, mode)
        st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
        raise VolumeError("私有文件卷内的目录被替换为符号链接或文件")


class BlobWriter:
    """流式写入一个文件。`finish` 校验后原子放入最终位置；任何异常都丢弃临时文件。"""

    def __init__(self, volume: PrivateVolume, limit: int) -> None:
        self._volume = volume
        self._limit = limit
        self._path = volume.root / SUBDIR / TMP / f"{uuid.uuid4().hex}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self._fd: int | None = os.open(self._path, flags, volume.file_mode)
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        if self._fd is None:
            raise VolumeError("写入器已关闭")
        self.size += len(data)
        if self.size > self._limit:
            self.discard()
            raise VolumeError("文件超过允许的大小")
        self._hash.update(data)
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]

    def discard(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._path.unlink(missing_ok=True)

    def finish(self, size: int, sha256: str) -> tuple[FileRef, bool]:
        """返回 (file_ref, 是否新写入)。大小或哈希不符时丢弃并抛 IntegrityError。"""
        if self._fd is None:
            raise VolumeError("写入器已关闭")
        try:
            if self.size != size or self._hash.hexdigest() != sha256:
                raise IntegrityError("收到的文件与声明的大小或 SHA-256 不一致")
            os.fsync(self._fd)
            os.close(self._fd)
            self._fd = None
            return self._volume._publish(self._path, sha256, size)
        finally:
            self.discard()


class PrivateVolume:
    def __init__(self, root: Path, *, file_mode: int = 0o640) -> None:
        if not root.is_absolute():
            raise VolumeError("文件卷根必须是绝对路径")
        try:
            st = os.lstat(root)
        except OSError:
            raise VolumeError("文件卷根不存在") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise VolumeError("文件卷根必须是真实目录")
        if st.st_mode & 0o022:
            raise VolumeError("文件卷根不得对其他用户可写")
        if not file_mode & 0o400 or file_mode & ~0o640:
            raise VolumeError("文件权限无效")
        self.root = root
        self.file_mode = file_mode
        self.dir_mode = file_mode | ((file_mode & 0o444) >> 2) | 0o700
        _check_dir(root / SUBDIR, self.dir_mode)
        for sub in (TMP, BLOBS):
            _check_dir(root / SUBDIR / sub, self.dir_mode)
        self.sweep_tmp()

    # -- layout ---------------------------------------------------------------------------
    @staticmethod
    def relative_path(sha256: str) -> str:
        return f"{SUBDIR}/{BLOBS}/{sha256[:2]}/{sha256[2:4]}/{sha256}"

    def _final(self, sha256: str) -> Path:
        return self.root / self.relative_path(sha256)

    def sweep_tmp(self) -> int:
        """删除超过一天的残留临时文件（进程在写入中途被杀留下的）。"""
        removed = 0
        directory = self.root / SUBDIR / TMP
        cutoff = time.time() - _STALE_TMP_SECONDS
        for entry in os.scandir(directory):
            if entry.name.endswith(".part") and entry.is_file(follow_symlinks=False):
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
        return removed

    # -- writing --------------------------------------------------------------------------
    def begin(self, limit: int) -> BlobWriter:
        return BlobWriter(self, limit)

    def put_bytes(self, data: bytes) -> tuple[FileRef, bool]:
        writer = self.begin(max(len(data), 1))
        writer.write(data)
        return writer.finish(len(data), hashlib.sha256(data).hexdigest())

    def _publish(self, temp: Path, sha256: str, size: int) -> tuple[FileRef, bool]:
        final = self._final(sha256)
        ref = FileRef(path=self.relative_path(sha256), sha256=sha256, size=size)
        _check_dir(final.parent.parent, self.dir_mode)
        _check_dir(final.parent, self.dir_mode)
        try:
            existing = os.lstat(final)
        except FileNotFoundError:
            existing = None
        if (
            existing is not None
            and stat.S_ISREG(existing.st_mode)
            and existing.st_size == size
            and self.verify(ref)
        ):
            return ref, False  # 同内容已在卷里：不重写
        os.replace(temp, final)
        os.chmod(final, self.file_mode)
        self._fsync_dir(final.parent)
        return ref, True

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- reading --------------------------------------------------------------------------
    def read_prefix(self, ref: FileRef, limit: int) -> bytes:
        """读取至多 limit 字节；路径必须是本卷按内容寻址的位置。"""
        if ref.path != self.relative_path(ref.sha256):
            raise VolumeError("file_ref 不在内容寻址位置")
        fd = os.open(self.root / ref.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            return os.read(fd, limit)
        finally:
            os.close(fd)

    def verify(self, ref: FileRef) -> bool:
        """整文件重新哈希，核对 file_ref 的 sha256 和 size。"""
        if ref.path != self.relative_path(ref.sha256):
            return False
        try:
            fd = os.open(self.root / ref.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        digest = hashlib.sha256()
        total = 0
        try:
            while chunk := os.read(fd, 1 << 20):
                digest.update(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
        return total == ref.size and digest.hexdigest() == ref.sha256
