"""只读盘点旧 Ear 与 Brain inbox；仅在输出目录写索引和时间线。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ear_adapter.convert import AUDIO_STAGES, Ready, readiness
from ear_adapter.ear_api import Capture, CaptureEvidence, SpeechSegment
from ear_adapter.source import EarSource, InboxAudioLocator

from ear_baselines.cases import DEFAULT_ROOT, write_json
from ear_baselines.evidence_md import render_evidence
from ear_baselines.media import MediaError, MediaInfo, probe

STATUSES = ("ready", "processing", "failed", "incomplete", "audio_missing", "not_ingested")
ACTIVE = {"pending", "running", "waiting_retry"}
FAILED = {"failed", "degraded", "cancelled"}
DEFAULT_DB = Path("~/.octopus/local/service/herryying30/octopus-service-ear/db/ear.sqlite3")
# 审计启发式，不是旧 Ear 的超时规则；超过远端允许的最长单任务 24h。
STALE_AFTER = timedelta(hours=48)
RERUN = (
    'POST /api/recordings/reprocess，JSON {"instance_id": <instance_id>, '
    '"record_id": <record_id>}；需要旧 Ear 管理接口权限、完整 Brain 源记录及音频；'
    "确认没有活动 run。analysis-rerun 仅重跑下游分析，不能修复感知结果。"
)


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"旧 Ear 时间没有时区：{value}")
    return result


def read_database(path: Path) -> dict[str, list[dict[str, Any]]]:
    """一个只读事务；不使用 immutable（它会漏掉 WAL 中的新任务）。"""
    connection = sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return {
            table: [
                json.loads(row[0]) for row in connection.execute(f"SELECT payload FROM {table}")
            ]
            for table in ("captures", "pipeline_runs", "pipeline_tasks", "remote_jobs")
        }
    finally:
        connection.close()


def failure(stage: str, message: str, *, stale: bool = False) -> dict[str, Any]:
    rerunnable = "unknown"
    prerequisite = "先核实并修复此错误；"
    reason = " ".join(message.split()) or "阶段失败，数据库没有记录错误详情"
    if "embedding_provenance" in message:
        reason = "说话人输出含 embedding_provenance，旧 Ear 模型校验不接受该字段"
        prerequisite = "先修复说话人输出与 SpeakerResolution 的字段兼容性；"
    elif "load_failed" in message or "encoder load failed" in message:
        reason = "说话人模型加载失败或被加载失败锁定"
        prerequisite = "修复说话人模型路径/依赖，并确认旧 Ear 模型已恢复可用；"
    elif "ConnectError" in message or "ConnectTimeout" in message:
        reason = "提交远端 MOSS 转写失败：连接不可达或连接超时"
        prerequisite = "远端 MOSS 服务必须在线且旧 Ear 可连接；"
        rerunnable = "yes"
    elif "audio event detection exceeded" in message:
        reason = "环境声检测超过 120 秒时限"
        prerequisite = "先确认 PANNs 可用、计算资源和超时配置足够；"
        rerunnable = "yes"
    if stale:
        prerequisite = (
            "这是超 48 小时无进展的疑似卡住任务；先人工核对远端任务，"
            "确认已停止后 POST /api/runs/<pipeline_run_id>/cancel，再重跑；"
        )
    return {
        "stage": stage,
        "reason": reason,
        "rerunnable": rerunnable,
        "rerun_how": prerequisite + RERUN,
        "error": message,
    }


def run_state(
    run: dict[str, Any],
    tasks: list[dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    now: datetime,
    tz: tzinfo,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只查询这个 run 的当前任务，历史失败不会污染新的成功 run。"""
    running: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for task in tasks:
        if task["status"] in FAILED:
            error = task.get("error") or {}
            failures.append(failure(task["stage"], error.get("message") or task["status"]))
    if run["status"] in ACTIVE:
        active = [t for t in tasks if t["status"] in ACTIVE]
        for task in active or [{"stage": "pipeline", "status": run["status"]}]:
            job = jobs.get(task.get("idempotency_key", ""), {})
            progress = [run["updated_at"], task.get("started_at"), job.get("last_processing_at")]
            last = max(_time(t) for t in progress if t)
            retry_at = job.get("next_retry_at")
            stale = now - last > STALE_AFTER and not (retry_at and _time(retry_at) > now)
            if stale:
                failures.append(failure(task["stage"], "任务超过 48 小时没有进展记录", stale=True))
            else:
                running.append(
                    {
                        "capture_id": run["capture_id"],
                        "stage": task["stage"],
                        "since": _time(task.get("started_at") or run["created_at"])
                        .astimezone(tz)
                        .isoformat(),
                        "pipeline_run_id": run["pipeline_run_id"],
                        "status": task["status"],
                        "remote_phase": job.get("phase"),
                    }
                )
    if run["status"] in FAILED and not failures:
        failures.append(failure("pipeline", f"run 状态为 {run['status']}，无阶段错误详情"))
    return running, failures


def classify(
    evidence: CaptureEvidence,
    audio_path: Path | None,
    running: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> tuple[str, list[str], list[dict[str, Any]]]:
    state = readiness(evidence)
    missing = [] if isinstance(state, Ready) else [state.reason]
    for entry in evidence.availability:
        if entry.type in AUDIO_STAGES and entry.state in {"failed", "partial"} and not failures:
            failures = [failure(entry.type, f"Evidence 标记 {entry.state}；数据库无对应错误详情")]
    if audio_path is None:
        return "audio_missing", [*missing, "Brain inbox 原始音频"], failures
    if running:
        return "processing", missing, failures
    if failures:
        return "failed", missing, failures
    return ("ready" if isinstance(state, Ready) else "incomplete"), missing, failures


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _counts(evidence: CaptureEvidence | None) -> tuple[dict[str, int], str]:
    counts: dict[str, int] = dict.fromkeys(
        ("speech_segments", "transcript_chars", "speakers", "audio_events", "gps", "phone_state"), 0
    )
    if evidence is None:
        return counts, ""
    speech = sorted(evidence.of_type("speech_segment", SpeechSegment), key=lambda s: s.started_at)
    text = "".join(s.original_text for s in speech)
    counts.update(
        speech_segments=len(speech),
        transcript_chars=len(text),
        speakers=len({(s.diarization_scope_id, s.local_speaker_id) for s in speech}),
    )
    gps = set()
    for item in evidence.evidence:
        if item.ref.type == "audio_event":
            counts["audio_events"] += 1
        if item.ref.type == "context_snapshot":
            counts["phone_state"] += 1
        if item.ref.type == "sensor_observation" and (loc := item.payload.get("location")):
            gps.add((_time(item.payload["observed_at"]), loc["latitude"], loc["longitude"]))
    counts["gps"] = len(gps)
    return counts, " ".join(text.split())[:80]


def scan_audio(root: Path) -> tuple[dict[str, list[Path]], dict[Path, MediaInfo], list[str]]:
    """按内容识别所有文件（不限扩展名）；失败项保留，不能静默当作非音频。"""
    hashes: dict[str, list[Path]] = defaultdict(list)
    media: dict[Path, MediaInfo] = {}
    skipped: list[str] = []
    inbox = root / "inbox" / "brain"
    if not inbox.is_dir():
        raise FileNotFoundError(f"Brain inbox 不存在：{inbox}")
    for path in sorted(inbox.rglob("*")):
        if not path.is_file():
            continue
        try:
            info = probe(path)
        except MediaError as exc:
            skipped.append(f"{path}: {exc}")
            continue
        media[path] = info
        hashes[_sha(path)].append(path)
    return hashes, media, skipped


def _check_out(out: Path, data_root: Path) -> None:
    """输出含真实转写：只能在 git 忽略的 local-data/ 或仓库外，且不能在旧 Ear 数据目录里。"""
    repo = Path(__file__).resolve().parents[4]
    in_repo = out.is_relative_to(repo) and not out.is_relative_to(DEFAULT_ROOT)
    if in_repo or out.is_relative_to(data_root):
        raise ValueError("输出必须在 local-data/ 或仓库外，且不在旧 Ear 数据目录里")


def build_inventory(
    source: EarSource,
    data_root: Path,
    database: Path,
    out: Path,
    tz: tzinfo,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    out = out.expanduser().resolve()
    _check_out(out, data_root.expanduser().resolve())
    data_root = data_root.expanduser().resolve()
    # 先完成输入读取；连接错误时不发布一个貌似完整的空索引。
    ids = [s.capture_id for s in source.list_captures()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        evidence_list = list(pool.map(source.read_capture, ids))
    db = read_database(database)
    hashes, media, skipped = scan_audio(data_root)
    locator = InboxAudioLocator(data_root)
    latest: dict[str, dict[str, Any]] = {}
    for run in sorted(db["pipeline_runs"], key=lambda r: _time(r["created_at"])):
        latest[run["capture_id"]] = run
    tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in db["pipeline_tasks"]:
        tasks[task["pipeline_run_id"]].append(task)
    jobs = {j["idempotency_key"]: j for j in db["remote_jobs"]}
    captures = {c["capture_id"]: Capture.model_validate(c) for c in db["captures"]}
    known_hashes = {c.audio_ref.sha256 for c in captures.values()}
    warnings = ["活动状态来自数据库快照，不是进程存活证明；48h 无进展判疑似卡住。"]
    missing_ids = set(captures) - set(ids)
    if missing_ids:
        raise ValueError(f"API 未列出数据库中的 {len(missing_ids)} 个 capture；请核对授权/并发变更")
    recordings = []
    running_now = []
    transcripts: dict[str, str] = {}
    for evidence in evidence_list:
        cid = evidence.capture_id
        values = evidence.of_type("capture", Capture)
        capture = values[0] if len(values) == 1 else captures.get(cid)
        paths: list[Path] = []
        if capture:
            known_hashes.add(capture.audio_ref.sha256)
            ref = locator.locate(capture)
            paths = hashes.get(capture.audio_ref.sha256, [])
            if ref:
                paths = list(dict.fromkeys([data_root / ref.path, *paths]))
        path = paths[0] if paths else None
        run = latest.get(cid)
        active, failures = (
            run_state(run, tasks[run["pipeline_run_id"]], jobs, now, tz) if run else ([], [])
        )
        for older in db["pipeline_runs"]:
            if older["capture_id"] == cid and older != run and older["status"] in ACTIVE:
                other_active, other_failures = run_state(
                    older, tasks[older["pipeline_run_id"]], jobs, now, tz
                )
                active.extend(other_active)
                failures.extend(other_failures)
                warnings.append(f"{cid}: 历史 run 仍标记活动状态，需核对是否遗留")
        running_now.extend(active)
        status, missing, failures = classify(evidence, path, active, failures)
        if run and evidence.pipeline_run_id != run["pipeline_run_id"]:
            warnings.append(f"{cid}: Evidence run 与数据库最新 run 不一致，需重新盘点")
            missing.append("Evidence 与数据库快照 run 不一致")
            if status == "ready":
                status = "incomplete"
        info = media.get(path) if path else None
        if path and not info:
            missing.append("ffprobe 未能读取音频；可定位文件但解码可用性未知")
            if status == "ready":
                status = "incomplete"
        start = evidence.started_at.astimezone(tz)
        duration = (
            info.duration_s
            if info
            else (
                (evidence.ended_at - evidence.started_at).total_seconds()
                if evidence.ended_at
                else 0.0
            )
        )
        counts, preview = _counts(evidence)
        ready_state = readiness(evidence)
        record = {
            "id": cid,
            "capture_id": cid,
            "status": status,
            "started_at": start.isoformat(),
            "local_date": start.date().isoformat(),
            "duration_s": duration,
            "audio_path": str(path) if path else None,
            "audio_paths": [str(p) for p in paths],
            "audio_codec": info.codec if info else "",
            "counts": counts,
            "preview": preview,
            "failure": failures[0] if failures else None,
            "failures": failures,
            "missing": missing,
            "instance_id": capture.instance_id if capture else None,
            "record_id": capture.record_id if capture else None,
            "pipeline_run_id": run["pipeline_run_id"] if run else None,
            "readiness": "Ready" if isinstance(ready_state, Ready) else ready_state.reason,
        }
        recordings.append(record)
        # 失败录音只要有转写（例如只是说话人或环境声阶段失败）也导出，便于收听和出题。
        if status in {"ready", "incomplete"} or counts["speech_segments"]:
            if Path(cid).name != cid or cid in {".", ".."}:
                raise ValueError("capture_id 不可用于文件名")
            transcripts[cid] = render_evidence(evidence, tz)
    for sha, paths in hashes.items():
        if sha in known_hashes:
            continue
        path, info = paths[0], media[paths[0]]
        start = info.creation_time
        basis = "container.creation_time"
        if start is None:
            try:
                start = datetime.strptime(
                    f"{path.parents[2].name} {path.parent.name.split('__')[0]}", "%Y-%m-%d %H-%M-%S"
                ).replace(tzinfo=tz)
                basis = "inbox 路径（假定设备当地时间为显示时区）"
            except ValueError:
                start = datetime.fromtimestamp(path.stat().st_mtime, tz)
                basis = "文件 mtime（录制时间未知）"
        start = start.astimezone(tz)
        counts, preview = _counts(None)
        recordings.append(
            {
                "id": f"file-{sha[:16]}",
                "capture_id": None,
                "status": "not_ingested",
                "started_at": start.isoformat(),
                "local_date": start.date().isoformat(),
                "started_at_basis": basis,
                "duration_s": info.duration_s,
                "audio_path": str(path),
                "audio_paths": [str(p) for p in paths],
                "audio_codec": info.codec,
                "counts": counts,
                "preview": preview,
                "failure": None,
                "failures": [],
                "missing": ["旧 Ear capture"],
            }
        )
    recordings.sort(key=lambda r: (_time(r["started_at"]), r["id"]))
    totals: dict[str, int | float] = {
        s: sum(r["status"] == s for r in recordings) for s in STATUSES
    }
    totals["ready_hours"] = (
        sum(r["duration_s"] for r in recordings if r["status"] == "ready") / 3600
    )
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for record in recordings:
        for f in record["failures"]:
            groups[f["reason"], f["rerunnable"], f["rerun_how"]].add(record["id"])
    result = {
        "generated_at": now.astimezone(tz).isoformat(),
        "tz": str(tz),
        "totals": totals,
        "running_now": running_now,
        "failure_groups": [
            {"reason": k[0], "count": len(v), "rerunnable": k[1], "rerun_how": k[2]}
            for k, v in sorted(groups.items())
        ],
        "recordings": recordings,
        "warnings": warnings,
        "unrecognized_files": skipped,
        "stale_after_hours": STALE_AFTER.total_seconds() / 3600,
    }
    out = out.expanduser().resolve()
    _check_out(out, data_root)
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    for cid, text in transcripts.items():
        (out / "transcripts" / f"{cid}.md").write_text(text, encoding="utf-8")
    # 只清理上次索引声明拥有、这次不再输出的时间线，避免前端读到旧成功结果。
    previous = out / "index.json"
    if previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8"))
        for row in old.get("recordings", []):
            cid = row.get("id", "")
            if cid and Path(cid).name == cid and cid not in {".", ".."} and cid not in transcripts:
                (out / "transcripts" / f"{cid}.md").unlink(missing_ok=True)
    write_json(out / "index.json", result)
    (out / "index.md").write_text(render_index(result), encoding="utf-8")
    return result


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_index(index: dict[str, Any]) -> str:
    lines = [
        "# 旧 Ear 录音素材索引",
        "",
        f"生成于 {index['generated_at']}；时区 {index['tz']}",
        "",
        "| 状态 | 段数 |",
        "|---|---:|",
    ]
    lines += [f"| {s} | {index['totals'][s]} |" for s in STATUSES]
    lines += [
        f"\n可用总时长：{index['totals']['ready_hours']:.2f} 小时。",
        "",
        "## 日期分布",
        "",
        "| 日期 | 段数 | 总小时 | 可用小时 |",
        "|---|---:|---:|---:|",
    ]
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in index["recordings"]:
        days[r["local_date"]].append(r)
    for day, rows in days.items():
        hours = sum(r["duration_s"] for r in rows) / 3600
        ready = sum(r["duration_s"] for r in rows if r["status"] == "ready") / 3600
        lines.append(f"| {day} | {len(rows)} | {hours:.2f} | {ready:.2f} |")
    lines += ["", "## 现在在跑的任务", "", "数据库快照中的排队/运行/等待重试；不保证进程存活。", ""]
    lines += [f"- {_cell(r)}" for r in index["running_now"]] or ["无。"]
    lines += [
        "",
        "## 失败原因",
        "",
        "同一段可有多个原因，各组按录音去重，组数不能相加。",
        "",
        "| 原因 | 段数 | 能否重跑 | 方法与前提 |",
        "|---|---:|---|---|",
    ]
    lines += [
        "| "
        + " | ".join(_cell(f[k]) for k in ("reason", "count", "rerunnable", "rerun_how"))
        + " |"
        for f in index["failure_groups"]
    ]
    lines += ["", "## 分类说明与待核实项", ""]
    lines += [f"- {_cell(w)}" for w in index["warnings"]]
    lines += [
        f"- 未识别文件 {len(index['unrecognized_files'])} 个；详见 index.json，可能包含损坏音频。"
    ]
    for day, rows in days.items():
        lines += [
            "",
            f"## {day}",
            "",
            "| 时间 | 分钟 | 状态 | 转写字数 | 预览 | 音频路径 | 缺项 |",
            "|---|---:|---|---:|---|---|---|",
        ]
        for r in rows:
            lines.append(
                "| "
                + " | ".join(
                    _cell(v)
                    for v in (
                        r["started_at"],
                        f"{r['duration_s'] / 60:.2f}",
                        r["status"],
                        r["counts"]["transcript_chars"],
                        r["preview"],
                        r["audio_path"],
                        "; ".join(r["missing"]),
                    )
                )
                + " |"
            )
    return "\n".join(lines) + "\n"
