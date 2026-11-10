"""实验样本（case）：一段录音及其各路线的输入、输出，都放在仓库外的实验目录里。

    <root>/cases/<case_id>/
        case.json            来源、哈希、时长、录制时间
        audio.m4a            统一成 16 kHz 单声道的录音（A1a、A1b 用）
        a2_evidence.md       A2：旧 Ear Evidence 渲染的时间线（只有 Octopus 录音有）
        a2_evidence.json     A2 解析后的 Evidence（只含 ② 用到的字段；排查用，不交给 Codex）
        a1a_listening.md     A1a：能听音频的模型写下的听写记录
        runs/<route>/<时间>/  每次运行 Codex 的工作区、答案和 run.json

case_id 由内容决定（文件的 SHA-256 或旧 Ear 的 capture_id），重复导入不会产生新 case。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from ear_baselines import media

DEFAULT_ROOT = Path("~/.ear-baselines")
AUDIO_NAME = "audio.m4a"
A2_EVIDENCE = "a2_evidence.md"
A2_EVIDENCE_JSON = "a2_evidence.json"
A1A_LISTENING = "a1a_listening.md"


def display_tz() -> tzinfo:
    """给 Codex 看的时刻用哪个时区：EAR_BASELINES_TZ（IANA 名，如 Asia/Shanghai），默认本机时区。

    旧 Ear 有时返回 UTC；按 UTC 显示会让模型把早上 7 点当成实际时刻，
    所以统一换成录音主人的当地时间。
    """
    name = os.environ.get("EAR_BASELINES_TZ")
    if name:
        return ZoneInfo(name)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


def default_root() -> Path:
    return Path(os.environ.get("EAR_BASELINES_ROOT", str(DEFAULT_ROOT))).expanduser()


class Case(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    source: Literal["file", "octopus"]
    original_path: str
    """原始录音的绝对路径（只记录，不复制原文件）。"""
    sha256: str
    duration_s: float
    codec: str
    started_at: datetime | None
    """录制开始时间（带时区）。文件来源取容器元数据，没有时为 None；Octopus 来源取旧 Ear。"""
    capture_id: str | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe(ident: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ident).strip("_")[:80]


class CaseStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cases_dir = root / "cases"

    def dir(self, case_id: str) -> Path:
        return self.cases_dir / case_id

    def load(self, case_id: str) -> Case:
        path = self.dir(case_id) / "case.json"
        if not path.exists():
            raise KeyError(f"没有这个 case：{case_id}（目录 {self.cases_dir}）")
        return Case.model_validate_json(path.read_text(encoding="utf-8"))

    def all(self) -> list[Case]:
        if not self.cases_dir.exists():
            return []
        return [
            Case.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self.cases_dir.glob("*/case.json"))
        ]

    def save(self, case: Case) -> Path:
        d = self.dir(case.case_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "case.json").write_text(case.model_dump_json(indent=2), encoding="utf-8")
        return d

    def add_audio(
        self,
        path: Path,
        *,
        source: Literal["file", "octopus"] = "file",
        capture_id: str | None = None,
        started_at: datetime | None = None,
    ) -> Case:
        """登记一段录音并生成统一格式的 audio.m4a。已登记过的直接返回原 case。"""
        path = path.resolve()
        digest = sha256_file(path)
        case_id = f"octopus-{_safe(capture_id)}" if capture_id else f"file-{digest[:16]}"
        d = self.dir(case_id)
        if (d / "case.json").exists() and (d / AUDIO_NAME).exists():
            return self.load(case_id)
        info = media.probe(path)
        case = Case(
            case_id=case_id,
            source=source,
            original_path=str(path),
            sha256=digest,
            duration_s=info.duration_s,
            codec=info.codec,
            started_at=started_at or info.creation_time,
            capture_id=capture_id,
        )
        media.normalize(path, d / AUDIO_NAME)
        self.save(case)
        return case


def discover(paths: list[Path]) -> tuple[list[Path], list[tuple[Path, str]]]:
    """展开目录，按内容挑出录音。返回（录音文件，[(跳过的文件, 原因)]）。"""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.is_file()))
        else:
            files.append(p)
    audio: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for f in files:
        if f.name.startswith("."):
            continue
        try:
            media.probe(f)
        except media.MediaError as exc:
            skipped.append((f, str(exc)))
            continue
        audio.append(f)
    return audio, skipped


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
