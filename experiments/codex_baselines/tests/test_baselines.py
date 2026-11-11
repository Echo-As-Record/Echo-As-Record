"""合成数据测试：ffmpeg 生成的正弦波录音、② 适配器的合成旧 Ear 响应。不调用 OpenAI 和 Codex。"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ear_adapter.ear_api import CaptureEvidence
from ear_adapter.testing import AUDIO_BYTES, FakeSource, evidence_json
from ear_baselines import codex_runner, media
from ear_baselines.cases import A2_EVIDENCE, AUDIO_NAME, CaseStore, discover
from ear_baselines.evidence_md import render_evidence
from ear_baselines.octopus import import_captures
from ear_baselines.openai_audio import listen

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


def _tone(path: Path, seconds: float, *extra: str) -> Path:
    """生成一段 44.1 kHz 双声道的测试录音（模拟非 16k 单声道的输入）。"""
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac",
            "2",
            *extra,
            str(path),
        ],
        check=True,
    )
    return path


@needs_ffmpeg
@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".flac", ".m4a"])
def test_any_format_is_normalized_to_16k_mono(tmp_path: Path, suffix: str) -> None:
    src = _tone(tmp_path / f"in{suffix}", 2)
    store = CaseStore(tmp_path / "root")
    case = store.add_audio(src)
    info = media.probe(store.dir(case.case_id) / AUDIO_NAME)
    assert (info.sample_rate, info.channels) == (16_000, 1)
    assert case.duration_s == pytest.approx(2, abs=0.1)
    assert case.case_id.startswith("file-")


@needs_ffmpeg
def test_creation_time_is_read_from_container(tmp_path: Path) -> None:
    # iPhone 语音备忘录在 m4a 容器里写 creation_time。
    src = _tone(tmp_path / "memo.m4a", 1, "-metadata", "creation_time=2026-10-26T01:55:38Z")
    case = CaseStore(tmp_path / "root").add_audio(src)
    assert case.started_at == datetime.fromisoformat("2026-10-26T01:55:38+00:00")


@needs_ffmpeg
def test_import_is_idempotent_and_detects_by_content(tmp_path: Path) -> None:
    audio_dir = tmp_path / "in"
    audio_dir.mkdir()
    _tone(audio_dir / "a.wav", 1)
    # 扩展名不对的录音也能识别；不是录音的文件被跳过。
    shutil.copy(audio_dir / "a.wav", audio_dir / "renamed.bin")
    (audio_dir / "notes.txt").write_text("not audio")
    found, skipped = discover([audio_dir])
    assert [p.name for p in found] == ["a.wav", "renamed.bin"]
    assert [p.name for p, _ in skipped] == ["notes.txt"]

    store = CaseStore(tmp_path / "root")
    first = [store.add_audio(p) for p in found]
    again = [store.add_audio(p) for p in found]
    assert first == again
    assert len(store.all()) == 1  # 两个文件内容相同 → 同一个 case


@needs_ffmpeg
def test_split_covers_whole_recording(tmp_path: Path) -> None:
    src = _tone(tmp_path / "long.wav", 25)
    chunks = media.split(src, tmp_path / "chunks", 10)
    assert len(chunks) == 3
    total = sum(media.probe(c).duration_s for c in chunks)
    assert total == pytest.approx(25, abs=0.3)


def test_not_audio_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "x.m4a"
    bad.write_bytes(b"definitely not audio")
    with pytest.raises(media.MediaError):
        media.probe(bad)


def test_evidence_markdown_is_a_readable_timeline() -> None:
    md = render_evidence(CaptureEvidence.model_validate(evidence_json()))
    assert "2026-10-26 10:55:38 – 11:25:38（+09:00" in md
    # 用模型原始输出，不用旧 Ear 里用户纠正过的文本。
    assert "合成发言一" in md and "用户纠正过的文本" not in md
    # 说话人按音频块编号；块2 的 S1 不和块1 的 S1 混在一起。
    assert "- 10:56:00–10:56:05 [块1·S1] 合成发言一" in md
    assert "- 11:12:00–11:12:09 [块2·S1] 合成发言三" in md
    assert "有声纹向量的说话人：块1·S1、块2·S1" in md
    # 环境声、位置（重复的点去重）、手机事件（推断的单独标出）。
    assert "[环境声] Keyboard" in md
    assert md.count("[位置] 35.00000, 139.00000（±12 m）") == 1
    assert "- 11:00:00 [手机] lock：true → false" in md
    assert "[手机·推断] music：false → true" in md
    # 按时间排序。
    times = [line.split(" ")[1][:8] for line in md.splitlines() if line.startswith("- 1")]
    assert times == sorted(times)


def test_evidence_markdown_uses_display_timezone() -> None:
    # 旧 Ear 可能返回 UTC；按录音主人的当地时区显示。
    ev = CaptureEvidence.model_validate(evidence_json())
    md = render_evidence(ev, ZoneInfo("Asia/Shanghai"))
    assert "2026-10-26 09:55:38 – 10:25:38（+08:00" in md
    assert "- 09:56:00–09:56:05 [块1·S1] 合成发言一" in md


@needs_ffmpeg
def test_octopus_import_gives_both_audio_and_evidence(tmp_path: Path) -> None:
    data_root = tmp_path / "octopus"
    audio = _tone(tmp_path / "seg.m4a", 1)
    ev = evidence_json()
    ev2 = evidence_json()
    ev2["capture_id"] = "cap-pending"
    ev2["analyzable"] = False

    class Locator:
        def locate(self, capture: Any):
            from ear_adapter.convert import FileRef

            target = data_root / "inbox" / "seg.m4a"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(audio, target)
            return FileRef(path="inbox/seg.m4a", sha256="x", size=len(AUDIO_BYTES))

    store = CaseStore(tmp_path / "root")
    done, skipped = import_captures(
        FakeSource({"cap-1": ev, "cap-pending": ev2}), Locator(), data_root, store
    )
    assert [c.case_id for c in done] == ["octopus-cap-1"]
    assert [s.capture_id for s in skipped] == ["cap-pending"]
    d = store.dir("octopus-cap-1")
    assert (d / AUDIO_NAME).exists() and (d / A2_EVIDENCE).exists()
    assert done[0].started_at == datetime.fromisoformat("2026-10-26T10:55:38+09:00")


@needs_ffmpeg
def test_listen_sends_each_chunk_with_absolute_times(tmp_path: Path) -> None:
    src = _tone(tmp_path / "memo.m4a", 25, "-metadata", "creation_time=2026-10-26T01:55:38Z")
    store = CaseStore(tmp_path / "root")
    case = store.add_audio(src)
    bodies: list[dict[str, Any]] = []

    def post(body: dict[str, Any]) -> dict[str, Any]:
        bodies.append(body)
        return {"choices": [{"message": {"content": f"段 {len(bodies)}"}}], "usage": {"n": 1}}

    path = listen(store, case, post=post, model="test-audio", chunk_seconds=10, tz=UTC)
    assert len(bodies) == 3
    first = bodies[0]["messages"][1]["content"]
    assert "01:55:38 到 01:55:48" in first[0]["text"]
    assert first[1]["type"] == "input_audio" and first[1]["input_audio"]["format"] == "mp3"
    assert base64.b64decode(first[1]["input_audio"]["data"])[:3] in (
        b"ID3",
        b"\xff\xfb",
        b"\xff\xf3",
    )
    text = path.read_text()
    assert "## 第 3 段：01:55:58 – 01:56:03" in text and "段 3" in text
    meta = json.loads((store.dir(case.case_id) / "a1a" / "meta.json").read_text())
    assert meta["chunks"] == 3


@needs_ffmpeg
def test_codex_workspace_contains_only_route_input(tmp_path: Path) -> None:
    store = CaseStore(tmp_path / "root")
    case = store.add_audio(_tone(tmp_path / "a.wav", 1))
    options = codex_runner.RunOptions(model="m", effort="low")
    run_dir = codex_runner.prepare(store, case, "a1b", options)
    ws = run_dir / "workspace"
    assert sorted(p.name for p in (ws / "input").iterdir()) == [
        "recording.m4a",
        "recording_info.md",
    ]
    assert "活动时间线" in (ws / "TASK.md").read_text()
    with pytest.raises(FileNotFoundError, match="a2_evidence"):
        codex_runner.prepare(store, case, "a2", options)

    cmd = codex_runner.codex_command(ws, "a1b", options, run_dir / "last.md")
    assert "--ignore-user-config" in cmd and cmd[cmd.index("-m") : cmd.index("-m") + 2] == [
        "-m",
        "m",
    ]
    # a1b 联网，a1c 不联网；两者输入相同。
    assert "sandbox_workspace_write.network_access=true" in cmd
    offline = codex_runner.codex_command(ws, "a1c", options, run_dir / "last.md")
    assert not any("network_access" in c for c in offline)
    offline_dir = codex_runner.prepare(store, case, "a1c", options)
    assert sorted(p.name for p in (offline_dir / "workspace" / "input").iterdir()) == [
        "recording.m4a",
        "recording_info.md",
    ]
