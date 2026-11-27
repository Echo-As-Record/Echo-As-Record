"""合成数据与测试替身（只含假数据；不含任何真实 LibreChat 内容）。

- `make_fixture`：造一个假的 LibreChat 应用目录（package.json、uploads/images 文件）和
  与 Mongo 文档同形状的 JSON 数据，供 `tools/librechat_export.mjs --fixture` 使用。
- `run_exporter`：用 Node 运行导出器并返回输出行（没有 node 时由调用方跳过测试）。
- 帧构造函数：直接拼合成的导出流，用来测试协议错误、截断和完整性失败。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ear_storage_client import MemoryStorage
from ear_storage_client.memory import MemoryClient

from librechat_adapter.config import EXPORT_FORMAT, REQUEST_FORMAT
from librechat_adapter.registry import with_chat_types

UID = "111111111111111111111111"
OTHER_UID = "222222222222222222222222"
REPO = Path(__file__).resolve().parents[4]
EXPORTER = REPO / "tools" / "librechat_export.mjs"
ROOT_PARENT = "00000000-0000-0000-0000-000000000000"

NOTES = b"alpha notes\nline two\n"
CHART = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
BIG = b"B" * 5000


def has_node() -> bool:
    return shutil.which("node") is not None


@dataclass(slots=True)
class LibreChatFixture:
    app: Path
    db: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def uploads(self) -> Path:
        return self.app / "uploads"

    @property
    def images(self) -> Path:
        return self.app / "client" / "public" / "images"

    @property
    def db_path(self) -> Path:
        return self.app.parent / "fixture-db.json"

    def write_db(self) -> None:
        self.db_path.write_text(json.dumps(self.db), encoding="utf-8")

    def message(self, message_id: str) -> dict[str, Any]:
        return next(m for m in self.db["messages"] if m["messageId"] == message_id)

    def file(self, file_id: str) -> dict[str, Any]:
        return next(f for f in self.db["files"] if f["file_id"] == file_id)


def make_fixture(tmp: Path, *, version: str = "v0.8.9") -> LibreChatFixture:
    app = tmp / "app"
    for directory in (
        app / "uploads" / UID,
        app / "uploads" / OTHER_UID,
        app / "client" / "public" / "images" / UID,
    ):
        directory.mkdir(parents=True)
    (app / "package.json").write_text(json.dumps({"name": "LibreChat", "version": version}))
    (app / "uploads" / UID / "f1__notes.txt").write_bytes(NOTES)
    (app / "uploads" / UID / "f6__big.bin").write_bytes(BIG)
    (app / "client" / "public" / "images" / UID / "chart.png").write_bytes(CHART)
    (app / "uploads" / OTHER_UID / "secret.txt").write_bytes(b"OTHER-USERS-SECRET")
    (app / "uploads" / UID / "link.txt").symlink_to(app / "uploads" / OTHER_UID / "secret.txt")

    def conv(cid: str, user: str, created: str, updated: str, title: str) -> dict[str, Any]:
        return {
            "conversationId": cid,
            "user": user,
            "title": title,
            "endpoint": "agents",
            "agent_id": "agent_demo",
            "tags": ["t1"],
            "createdAt": created,
            "updatedAt": updated,
            "isTemporary": False,
            "isArchived": False,
            "expiredAt": None,
        }

    def msg(mid: str, cid: str, user: str, at: str, text: str, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "messageId": mid,
            "conversationId": cid,
            "user": user,
            "parentMessageId": ROOT_PARENT,
            "sender": "User",
            "text": text,
            "isCreatedByUser": True,
            "createdAt": at,
            "updatedAt": at,
        }
        return {**base, **extra}

    def file_doc(fid: str, path: str, mtype: str, size: int, **extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "user": UID,
            "file_id": fid,
            "conversationId": "conv-1",
            "messageId": "m1",
            "filename": fid + ".dat",
            "filepath": path,
            "bytes": size,
            "type": mtype,
            "source": "local",
            "context": "message_attachment",
            "createdAt": "2026-11-01T10:00:00.500Z",
            "updatedAt": "2026-11-01T10:00:00.500Z",
        }
        return {**base, **extra}

    db = {
        "conversations": [
            conv(
                "conv-1", UID, "2026-11-01T10:00:00.000Z", "2026-11-01T10:05:00.000Z", "合成会话 A"
            ),
            conv("conv-2", UID, "2026-10-01T08:00:00.000Z", "2026-10-01T08:01:00.000Z", "旧会话"),
            conv(
                "conv-other",
                OTHER_UID,
                "2026-11-01T09:00:00.000Z",
                "2026-11-01T09:00:00.000Z",
                "别人",
            ),
        ],
        "messages": [
            msg(
                "m1",
                "conv-1",
                UID,
                "2026-11-01T10:00:01.000Z",
                "请阅读附件 notes.txt",
                files=[
                    {
                        "file_id": "f-1",
                        "filename": "notes.txt",
                        "filepath": f"/uploads/{UID}/f1__notes.txt",
                    }
                ],
            ),
            msg(
                "m2",
                "conv-1",
                UID,
                "2026-11-01T10:00:05.000Z",
                "",
                parentMessageId="m1",
                sender="Assistant",
                isCreatedByUser=False,
                model="demo-model",
                endpoint="agents",
                updatedAt="2026-11-01T10:00:09.000Z",
                content=[
                    {"type": "think", "think": "内部思考 INNER"},
                    {"type": "text", "text": "好的，我来看看。"},
                    {
                        "type": "tool_call",
                        "tool_call": {
                            "name": "web_search",
                            "args": '{"q":"x"}',
                            "output": "R" * 50,
                        },
                    },
                    {"type": "text", "text": "附件内容是 alpha。"},
                ],
                attachments=[
                    {
                        "file_id": "f-2",
                        "filename": "chart.png",
                        "filepath": f"/images/{UID}/chart.png",
                        "type": "image/png",
                    },
                    {"type": "web_search", "web_search": {"results": ["not a file"]}},
                ],
                privateText="TOP-SECRET-PRIVATE",
                subagentTranscript={"messagesJson": "TOP-SECRET-SUBAGENT"},
            ),
            msg(
                "m3",
                "conv-1",
                UID,
                "2026-11-01T10:05:00.000Z",
                "谢谢",
                parentMessageId="m2",
                files=[
                    {"file_id": fid, "filename": fid}
                    for fid in ("f-3", "f-4", "f-5", "f-6", "f-7", "f-8")
                ],
            ),
            msg("m4", "conv-2", UID, "2026-10-01T08:00:30.000Z", "很早以前的消息"),
            msg("m-other", "conv-other", OTHER_UID, "2026-11-01T09:00:10.000Z", "OTHER-USER-TEXT"),
        ],
        "files": [
            file_doc(
                "f-1",
                f"/uploads/{UID}/f1__notes.txt",
                "text/plain; charset=utf-8",  # 媒体类型参数会被去掉
                len(NOTES),
                filename="notes.txt",
            ),
            file_doc(
                "f-2",
                f"/images/{UID}/chart.png",
                "image/png",
                len(CHART),
                filename="chart.png",
                messageId="m2",
                context="execute_code",
                status="pending",  # 预览状态不影响字节导出
            ),
            file_doc("f-3", f"s3://bucket/{UID}/x", "application/pdf", 10, source="s3"),
            file_doc("f-4", f"/uploads/{OTHER_UID}/secret.txt", "text/plain", 18, messageId="m3"),
            file_doc("f-5", f"/uploads/{UID}/link.txt", "text/plain", 18, messageId="m3"),
            file_doc("f-6", f"/uploads/{UID}/f6__big.bin", "application/octet-stream", len(BIG)),
            file_doc("f-8", f"/uploads/{UID}/gone.txt", "text/plain", 5, messageId="m3"),
            {
                **file_doc("f-7", f"/uploads/{OTHER_UID}/secret.txt", "text/plain", 18),
                "user": OTHER_UID,
            },
        ],
    }
    fixture = LibreChatFixture(app, db)
    fixture.write_db()
    return fixture


def run_exporter(
    fixture: LibreChatFixture,
    *,
    mode: str = "full",
    since: str | None = None,
    extra: Sequence[str] = (),
    user_id: str = UID,
) -> tuple[int, list[str]]:
    """运行 Node 导出器，返回 (退出码, 输出行)。"""
    request = json.dumps({"format": REQUEST_FORMAT, "mode": mode, "since": since})
    proc = subprocess.run(
        [
            "node",
            str(EXPORTER),
            "--fixture",
            str(fixture.db_path),
            "--app-dir",
            str(fixture.app),
            "--user-id",
            user_id,
            "--request-stdin",
            "--pause-ms",
            "0",
            *extra,
        ],
        input=request.encode(),
        capture_output=True,
        timeout=60,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )
    return proc.returncode, proc.stdout.decode().splitlines(keepends=True)


# -- synthetic frames --------------------------------------------------------------------------
def line(**frame: Any) -> str:
    return json.dumps(frame, ensure_ascii=False) + "\n"


def header(version: str = "0.8.9", mode: str = "full", **extra: Any) -> str:
    return line(
        t="header",
        format=EXPORT_FORMAT,
        exporter_version="1",
        librechat_version=version,
        mode=mode,
        since=None,
        started_at="2026-11-02T00:00:00.000Z",
        account_ref="0" * 16,
        **extra,
    )


def end(watermark: str = "2026-11-01T23:58:00.000Z", **extra: Any) -> str:
    return line(t="end", complete=True, counts={}, watermark=watermark, **extra)


def conversation(cid: str = "c1", title: str = "标题", **extra: Any) -> str:
    return line(
        t="conversation",
        conversation_id=cid,
        title=title,
        created_at="2026-11-01T10:00:00.000Z",
        updated_at="2026-11-01T10:00:00.000Z",
        **extra,
    )


def message(mid: str = "m1", cid: str = "c1", text: str = "你好", **extra: Any) -> str:
    fields: dict[str, Any] = {
        "t": "message",
        "conversation_id": cid,
        "message_id": mid,
        "role": "user",
        "created_at": "2026-11-01T10:00:01.000Z",
        "updated_at": "2026-11-01T10:00:01.000Z",
        "text": text,
    }
    return line(**{**fields, **extra})


def attachment(
    file_id: str = "f1",
    *,
    mid: str = "m1",
    cid: str = "c1",
    blob_id: int | None = None,
    unavailable: str | None = None,
    media_type: str = "text/plain",
    filename: str | None = None,
    **extra: Any,
) -> str:
    fields: dict[str, Any] = {
        "t": "attachment",
        "conversation_id": cid,
        "message_id": mid,
        "message_created_at": "2026-11-01T10:00:01.000Z",
        "ref_kind": "upload",
        "file_id": file_id,
        "filename": filename if filename is not None else f"{file_id}.txt",
        "media_type": media_type,
        "blob_id": blob_id,
        "unavailable": unavailable,
    }
    return line(**{**fields, **extra})


def blob(blob_id: int, data: bytes, *, chunk: int = 1000, sha256: str | None = None) -> list[str]:
    frames = [
        line(t="blob", blob_id=blob_id, seq=i, b64=base64.b64encode(data[o : o + chunk]).decode())
        for i, o in enumerate(range(0, len(data), chunk))
    ]
    digest = sha256 or hashlib.sha256(data).hexdigest()
    return [*frames, line(t="blob_end", blob_id=blob_id, size=len(data), sha256=digest)]


def memory_storage() -> tuple[MemoryStorage, MemoryClient]:
    """带聊天类型登记的内存 ①；只在本夹具显式启用；默认登记表不变。"""
    storage = MemoryStorage(registry=with_chat_types())
    return storage, storage.client("adapter")


def join(parts: Iterable[str | Sequence[str]]) -> list[str]:
    out: list[str] = []
    for part in parts:
        out.extend([part] if isinstance(part, str) else part)
    return out
