"""副作用的边缘：读旧 Ear 的 Evidence API、在 Brain inbox 里定位音频文件。"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from ear_adapter.convert import FileRef
from ear_adapter.ear_api import Capture, CaptureEvidence, CaptureSummary, Embedding


class SourceUnavailable(Exception):
    """旧 Ear 暂时连不上或返回 5xx。runtime 记下原因，下一轮再试，不当成数据错误。"""


class EarSource(Protocol):
    def list_captures(self) -> Iterator[CaptureSummary]: ...
    def read_capture(self, capture_id: str) -> CaptureEvidence: ...
    def read_embedding(self, resolution_id: str) -> Embedding: ...


class HttpEarSource:
    """旧 Ear 的 Evidence 只读 API（需要 Evidence-read 授权的 bearer token）。

    base_url 直接指向旧 Ear 进程（例如 http://127.0.0.1:8765/internal），不经过 Octopus
    的服务代理：代理在子进程重启后可能仍指向旧端口，见 ai-docs/implementation.md。
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise SourceUnavailable(f"旧 Ear 返回 {exc.code}：{path}") from exc
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise SourceUnavailable(f"连不上旧 Ear：{exc}") from exc

    def list_captures(self) -> Iterator[CaptureSummary]:
        # 旧 Ear 的 GET /api/captures（limit ≤ 500），见 ai-docs/implementation.md 第 3 节。
        offset, limit = 0, 100
        while True:
            page = self._get("/api/captures", {"offset": offset, "limit": limit})
            items = page["items"]
            for item in items:
                yield CaptureSummary.model_validate(item)
            offset += len(items)
            if not items or offset >= page["total"]:
                return

    def read_capture(self, capture_id: str) -> CaptureEvidence:
        quoted = urllib.parse.quote(capture_id, safe="")
        return CaptureEvidence.model_validate(self._get(f"/api/captures/{quoted}/evidence"))

    def read_embedding(self, resolution_id: str) -> Embedding:
        quoted = urllib.parse.quote(resolution_id, safe="")
        return Embedding.model_validate(
            self._get(f"/api/evidence/speaker_resolution/{quoted}/embedding")
        )


class AudioLocator(Protocol):
    def locate(self, capture: Capture) -> FileRef | None: ...


class InboxAudioLocator:
    """在 Brain inbox 里找到录音的音频文件，返回相对 data_root 的引用。

    旧 Ear 的公开 API 不给文件路径。按 inbox 布局
    inbox/brain/{instance_id}/{手机当地日期}/{collection}/{HH-MM-SS}__{group_id}/
    按组 id 缩小范围（不限日期），
    再用大小和 SHA-256 确认是同一个文件。找不到时返回 None（下一轮再试）。
    """

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def locate(self, capture: Capture) -> FileRef | None:
        expected = capture.audio_ref
        instance_dir = self.data_root / "inbox" / "brain" / capture.instance_id
        # 日期目录是手机当地日期，和录音的 UTC 日期、旧 Ear 返回的时区都可能差一天；
        # 组 id 是 UUID，不按日期缩小范围。
        for group_dir in sorted(instance_dir.glob(f"*/*/*__{capture.group_id}")):
            for path in sorted(group_dir.iterdir()):
                if not path.is_file() or path.stat().st_size != expected.size_bytes:
                    continue
                if _sha256(path) == expected.sha256:
                    relative = path.relative_to(self.data_root).as_posix()
                    return FileRef(path=relative, sha256=expected.sha256, size=expected.size_bytes)
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
