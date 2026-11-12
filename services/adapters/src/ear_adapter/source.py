"""副作用的边缘：鉴权 Evidence API 与只读、限定范围的原位音频 hash。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from email.message import Message
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from ear_adapter.convert import FileRef
from ear_adapter.ear_api import Capture, CaptureEvidence, CaptureSummary, Embedding
from ear_adapter.selection import InvalidCaptureSelection, validate_capture_id


class SourceUnavailable(Exception):
    """旧 Ear 暂时连不上或返回 5xx，下一轮再试。"""


class SourceProtocolError(Exception):
    def __init__(self) -> None:
        super().__init__("旧 Ear 协议错误；检查响应格式及请求标识一致性")


class AudioAccessError(Exception):
    def __init__(self) -> None:
        super().__init__("音频定位安全检查失败；检查只读根目录、媒体类型及文件元数据")


class EarSource(Protocol):
    def list_captures(self) -> Iterator[CaptureSummary]: ...
    def read_capture(self, capture_id: str) -> CaptureEvidence: ...
    def read_embedding(self, resolution_id: str) -> Embedding: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不向任何跳转地址发送 Authorization，连同主机/端口也不例外。
        return None


def _model[M: BaseModel](model: type[M], value: Any) -> M:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise SourceProtocolError() from None


class HttpEarSource:
    """旧 Ear Evidence API；专属 opener，禁用环境代理和所有重定向。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        try:
            parsed = urllib.parse.urlsplit(base_url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base_url)
                or any(c in "\r\n" for c in token)
            ):
                raise ValueError
            _ = parsed.port
        except ValueError:
            raise SourceProtocolError() from None
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            with self._opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise SourceProtocolError()
                return json.load(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code >= 500:
                raise SourceUnavailable("旧 Ear 暂时不可用") from None
            # 保持 401/403/404 等 HTTPError 分类，但丢弃 reason/URL/headers/body。
            raise urllib.error.HTTPError(
                "", code, "旧 Ear HTTP 请求被拒绝", Message(), None
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise SourceUnavailable("旧 Ear 暂时不可用") from None
        except (ValueError, http.client.HTTPException):
            raise SourceProtocolError() from None

    def list_captures(self) -> Iterator[CaptureSummary]:
        offset, limit = 0, 100
        while True:
            page = self._get("/api/captures", {"offset": offset, "limit": limit})
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("items"), list)
                or type(page.get("total")) is not int
                or page["total"] < 0
            ):
                raise SourceProtocolError()
            items = page["items"]
            for item in items:
                summary = _model(CaptureSummary, item)
                try:
                    validate_capture_id(summary.capture_id)
                except InvalidCaptureSelection:
                    raise SourceProtocolError() from None
                yield summary
            offset += len(items)
            if not items or offset >= page["total"]:
                return

    def read_capture(self, capture_id: str) -> CaptureEvidence:
        quoted = urllib.parse.quote(validate_capture_id(capture_id), safe="")
        return _model(CaptureEvidence, self._get(f"/api/captures/{quoted}/evidence"))

    def read_embedding(self, resolution_id: str) -> Embedding:
        quoted = urllib.parse.quote(validate_capture_id(resolution_id), safe="")
        return _model(Embedding, self._get(f"/api/evidence/speaker_resolution/{quoted}/embedding"))


class AudioLocator(Protocol):
    def locate(self, capture: Capture) -> FileRef | None: ...


# 不回退扫描所有文件。未知格式需明确评审后加入；不打开同目录 ASR/JSON 等。
_AUDIO_SUFFIXES = {
    "audio/mp4": frozenset({".m4a", ".mp4"}),
    "audio/mpeg": frozenset({".mp3"}),
    "audio/wav": frozenset({".wav"}),
    "audio/x-wav": frozenset({".wav"}),
    "audio/flac": frozenset({".flac"}),
    "audio/ogg": frozenset({".ogg", ".opus"}),
    "audio/aac": frozenset({".aac"}),
    "audio/webm": frozenset({".webm"}),
}


def _safe_component(value: str) -> bool:
    try:
        validate_capture_id(value)
    except InvalidCaptureSelection:
        return False
    return not any(c in "*?[]" for c in value)


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_mode) == (b.st_dev, b.st_ino, b.st_mode)


@contextmanager
def _directory(
    name: str,
    parent: int | None = None,
    chain: list[tuple[int | None, str, int]] | None = None,
) -> Iterator[int | None]:
    """No-follow open; close even when the post-open identity check fails."""
    fd = None
    added = False
    try:
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                if not _same_file(before, os.fstat(fd)):
                    raise AudioAccessError()
        except (FileNotFoundError, NotADirectoryError):
            if fd is not None:
                os.close(fd)
                fd = None
        if fd is not None and chain is not None:
            chain.append((parent, name, fd))
            added = True
        yield fd
    finally:
        if added:
            assert chain is not None
            chain.pop()
        if fd is not None:
            os.close(fd)


def _check_chain(chain: list[tuple[int | None, str, int]]) -> None:
    """Detect detached/replaced ancestors before reading; not a filesystem lock."""
    for parent, name, fd in chain:
        linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same_file(linked, os.fstat(fd)):
            raise AudioAccessError()


class InboxAudioLocator:
    """固定深度 inbox/brain/实例/手机日期/collection/时间__组/音频。

    POSIX dir_fd + O_NOFOLLOW 必需，不支持时 fail closed。根路径各级也禁止 symlink/..。
    部署必须是可信的只读视图：不保证抵抗挂载替换或目录被并发搬出根，返回的路径引用
    也不是永久文件句柄。媒体类型/扩展名白名单不是音频解码器；仍须大小和完整 hash 匹配。
    """

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def locate(self, capture: Capture) -> FileRef | None:
        expected = capture.audio_ref
        suffixes = _AUDIO_SUFFIXES.get(expected.media_type)
        if (
            not _safe_component(capture.instance_id)
            or not _safe_component(capture.group_id)
            or ".." in self.data_root.parts
            or not suffixes
            or expected.size_bytes < 0
            or re.fullmatch(r"[0-9a-f]{64}", expected.sha256) is None
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
            or os.listdir not in os.supports_fd
        ):
            raise AudioAccessError()
        try:
            with ExitStack() as stack:
                chain: list[tuple[int | None, str, int]] = []
                # 不 resolve（会跟随链接），从文件系统根逐级 no-follow。
                root = self.data_root.absolute()
                fd = stack.enter_context(_directory(root.anchor, chain=chain))
                for part in (*root.parts[1:], "inbox", "brain", capture.instance_id):
                    if fd is None:
                        return None
                    fd = stack.enter_context(_directory(part, fd, chain))
                if fd is None:
                    return None
                prefix = Path("inbox", "brain", capture.instance_id)
                return self._walk(fd, prefix, capture, suffixes, chain=chain, depth=0)
        except OSError:
            # 路径、文件名及 OS 错误正文不进入日志；不转为正常成功。
            raise AudioAccessError() from None

    def _walk(
        self,
        fd: int,
        relative: Path,
        capture: Capture,
        suffixes: frozenset[str],
        *,
        chain: list[tuple[int | None, str, int]],
        depth: int,
    ) -> FileRef | None:
        _check_chain(chain)
        names = sorted(os.listdir(fd))
        _check_chain(chain)
        for name in names:
            if not _safe_component(name):
                continue
            if depth == 2 and not name.endswith("__" + capture.group_id):
                continue
            if depth == 3:
                if Path(name).suffix.lower() not in suffixes:
                    continue
                if self._matches(fd, name, capture, check=lambda: _check_chain(chain)):
                    return FileRef(
                        path=(relative / name).as_posix(),
                        sha256=capture.audio_ref.sha256,
                        size=capture.audio_ref.size_bytes,
                    )
            else:
                with _directory(name, fd, chain) as child:
                    if child is not None:
                        found = self._walk(
                            child, relative / name, capture, suffixes, chain=chain, depth=depth + 1
                        )
                        if found is not None:
                            return found
        return None

    @staticmethod
    def _matches(parent: int, name: str, capture: Capture, *, check: Callable[[], None]) -> bool:
        expected = capture.audio_ref
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != expected.size_bytes
            ):
                return False
            # NONBLOCK 防止预检后被换成 FIFO 而挂死；打开后仍先 fstat 再读字节。
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                opened = os.fstat(fd)
                check()
                if not _same_file(before, opened) or _signature(before) != _signature(opened):
                    return False
                digest = _sha256_fd(fd, expected.size_bytes)
                after = os.fstat(fd)
                check()
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                return digest == expected.sha256 and _signature(opened) == _signature(
                    after
                ) == _signature(current)
            finally:
                os.close(fd)
        except (FileNotFoundError, NotADirectoryError):
            return False


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _sha256_fd(fd: int, size: int) -> str | None:
    h = hashlib.sha256()
    read = 0
    while block := os.read(fd, min(1 << 20, size - read + 1)):
        read += len(block)
        if read > size:
            return None
        h.update(block)
    return h.hexdigest() if read == size else None
