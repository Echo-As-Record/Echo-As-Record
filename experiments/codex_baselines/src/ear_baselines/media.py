"""ffprobe / ffmpeg：识别任意格式的录音，统一成 16 kHz 单声道，按需切片。

按内容识别（ffprobe 能读出音频流就算录音），不看扩展名：iPhone 语音备忘录（.m4a / .qta）、
wav、mp3、flac、ogg/opus、视频里的音轨都能进来。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SAMPLE_RATE = 16_000


class MediaError(Exception):
    """文件不是可解码的录音，或 ffmpeg 处理失败。"""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration_s: float
    codec: str
    sample_rate: int
    channels: int
    container: str
    creation_time: datetime | None
    """容器里记录的录制时间（iPhone 语音备忘录有，带时区）；没有时为 None。"""


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise MediaError(f"找不到 {tool}，请先安装 ffmpeg（brew install ffmpeg）")
    return path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-3:]
        raise MediaError(f"{Path(args[0]).name} 失败：{' / '.join(tail)}")
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def probe(path: Path) -> MediaInfo:
    """读出第一条音频流的信息；没有音频流或解不开时抛 MediaError。"""
    out = _run(
        [
            _require("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(out.stdout)
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise MediaError(f"没有音频流：{path.name}")
    stream = audio[0]
    fmt = data.get("format", {})
    duration = stream.get("duration") or fmt.get("duration")
    if duration is None:
        raise MediaError(f"读不出时长：{path.name}")
    tags = {**fmt.get("tags", {}), **stream.get("tags", {})}
    return MediaInfo(
        duration_s=float(duration),
        codec=str(stream.get("codec_name", "")),
        sample_rate=int(stream.get("sample_rate", 0)),
        channels=int(stream.get("channels", 0)),
        container=str(fmt.get("format_name", "")),
        creation_time=_parse_time(tags.get("creation_time") or tags.get("date")),
    )


def normalize(src: Path, dst: Path) -> Path:
    """转成 16 kHz、单声道。dst 的扩展名决定编码：.wav（PCM）、.mp3、.m4a（AAC）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    codec = {
        ".wav": ["-c:a", "pcm_s16le"],
        ".mp3": ["-c:a", "libmp3lame", "-b:a", "48k"],
        ".m4a": ["-c:a", "aac", "-b:a", "48k"],
    }.get(dst.suffix)
    if codec is None:
        raise MediaError(f"不支持的输出格式：{dst.suffix}")
    _run(
        [
            _require("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            *codec,
            str(dst),
        ]
    )
    return dst


def split(src: Path, out_dir: Path, seconds: float, suffix: str = ".mp3") -> list[Path]:
    """切成每段 seconds 秒（最后一段可能更短），统一成 16 kHz 单声道。返回按时间排序的文件。"""
    if seconds <= 0:
        raise ValueError("seconds 必须大于 0")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"chunk_*{suffix}"):
        old.unlink()
    codec = ["-c:a", "libmp3lame", "-b:a", "48k"] if suffix == ".mp3" else ["-c:a", "pcm_s16le"]
    _run(
        [
            _require("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            *codec,
            "-f",
            "segment",
            "-segment_time",
            str(seconds),
            "-reset_timestamps",
            "1",
            str(out_dir / f"chunk_%04d{suffix}"),
        ]
    )
    return sorted(out_dir.glob(f"chunk_*{suffix}"))
