"""从旧 Ear 取 Octopus 录音：同一段录音同时得到原始音频（A1）和结构化证据（A2）。

三条路线要在同一批录音上比较，所以 Octopus 录音是共同样本；任意格式的本地录音只能走 A1。
复用 ② 适配器的旧 Ear 客户端、完整性判断和音频定位，只读，不写 ①。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path

from ear_adapter.convert import Ready, readiness
from ear_adapter.source import AudioLocator, EarSource

from ear_baselines.cases import A2_EVIDENCE, A2_EVIDENCE_JSON, Case, CaseStore, write_json
from ear_baselines.evidence_md import render_evidence


@dataclass(frozen=True, slots=True)
class Skipped:
    capture_id: str
    reason: str


def import_captures(
    source: EarSource,
    locator: AudioLocator,
    data_root: Path,
    store: CaseStore,
    capture_ids: Iterable[str] | None = None,
    limit: int | None = None,
    tz: tzinfo | None = None,
) -> tuple[list[Case], list[Skipped]]:
    """只导入旧 Ear 已完整处理（与 ② 相同的标准）且能找到原始音频的录音。"""
    ids = list(capture_ids) if capture_ids is not None else None
    if ids is None:
        ids = [c.capture_id for c in source.list_captures()]
    done: list[Case] = []
    skipped: list[Skipped] = []
    for capture_id in ids:
        if limit is not None and len(done) >= limit:
            break
        evidence = source.read_capture(capture_id)
        state = readiness(evidence)
        if not isinstance(state, Ready):
            skipped.append(Skipped(capture_id, state.reason))
            continue
        ref = locator.locate(state.capture)
        if ref is None:
            skipped.append(Skipped(capture_id, "在 Brain inbox 里找不到原始音频"))
            continue
        case = store.add_audio(
            data_root / ref.path,
            source="octopus",
            capture_id=capture_id,
            started_at=evidence.started_at,
        )
        d = store.dir(case.case_id)
        (d / A2_EVIDENCE).write_text(render_evidence(evidence, tz), encoding="utf-8")
        write_json(d / A2_EVIDENCE_JSON, evidence.model_dump(mode="json"))
        done.append(case)
    return done, skipped
