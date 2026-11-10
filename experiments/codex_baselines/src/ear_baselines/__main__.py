"""命令行：ear-baselines <子命令>。凭据只从环境变量读（deploy/.env）。

    ear-baselines import ~/Downloads/语音备忘录/          任意格式录音 → case（A1 用）
    ear-baselines octopus --limit 5                       旧 Ear 的 Octopus 录音 → case（A1、A2 用）
    ear-baselines listen <case_id>…                       A1a：gpt-audio 听写
    ear-baselines run a2 <case_id>…                       用 Codex 跑一条路线（a1a / a1b / a2）
    ear-baselines list                                    列出 case 和已有的输入、运行次数

实验目录默认 ~/.ear-baselines，可用 EAR_BASELINES_ROOT 改；给模型看的时刻按
EAR_BASELINES_TZ（如 Asia/Shanghai，默认本机时区）显示。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ear_baselines import codex_runner
from ear_baselines.cases import (
    A1A_LISTENING,
    A2_EVIDENCE,
    CaseStore,
    default_root,
    discover,
    display_tz,
)


def _store(args: argparse.Namespace) -> CaseStore:
    return CaseStore(Path(args.root).expanduser() if args.root else default_root())


def _cases(store: CaseStore, ids: list[str], all_: bool):
    if all_:
        return store.all()
    if not ids:
        raise SystemExit("给出 case_id，或者用 --all")
    return [store.load(i) for i in ids]


def cmd_import(args: argparse.Namespace) -> int:
    store = _store(args)
    audio, skipped = discover([Path(p).expanduser() for p in args.paths])
    for path in audio:
        case = store.add_audio(path)
        print(f"{case.case_id}\t{case.duration_s / 60:.1f} min\t{case.codec}\t{path}")
    for path, reason in skipped:
        print(f"跳过\t{path}\t{reason}", file=sys.stderr)
    print(f"导入 {len(audio)} 段，跳过 {len(skipped)} 个文件；目录 {store.cases_dir}")
    return 0


def cmd_octopus(args: argparse.Namespace) -> int:
    from ear_adapter.source import HttpEarSource, InboxAudioLocator

    from ear_baselines.octopus import import_captures

    url = os.environ.get("EAR_ADAPTER_EAR_URL")
    token = os.environ.get("EAR_ADAPTER_EAR_TOKEN")
    if not url or not token:
        print("需要环境变量 EAR_ADAPTER_EAR_URL 和 EAR_ADAPTER_EAR_TOKEN", file=sys.stderr)
        return 2
    data_root = Path(os.environ.get("EAR_ADAPTER_DATA_ROOT", "~/.octopus/local")).expanduser()
    store = _store(args)
    done, skipped = import_captures(
        HttpEarSource(url, token),
        InboxAudioLocator(data_root),
        data_root,
        store,
        capture_ids=args.capture or None,
        limit=args.limit,
        tz=display_tz(),
    )
    for case in done:
        print(f"{case.case_id}\t{case.duration_s / 60:.1f} min\t{case.started_at}")
    for s in skipped:
        print(f"跳过\t{s.capture_id}\t{s.reason}", file=sys.stderr)
    print(f"导入 {len(done)} 段，跳过 {len(skipped)} 段；目录 {store.cases_dir}")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    from ear_baselines.openai_audio import listen

    store = _store(args)
    for case in _cases(store, args.case_ids, args.all):
        path = listen(store, case, model=args.model, chunk_seconds=args.chunk_seconds)
        print(f"{case.case_id}\t{path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    store = _store(args)
    options = codex_runner.RunOptions(
        model=args.model,
        effort=args.effort,
        network=args.network,
        timeout_s=args.timeout,
        task=Path(args.task).expanduser() if args.task else None,
    )
    failed = 0
    for case in _cases(store, args.case_ids, args.all):
        try:
            run_dir = codex_runner.run(store, case, args.route, options)
        except FileNotFoundError as exc:
            print(f"跳过\t{exc}", file=sys.stderr)
            failed += 1
            continue
        answered = (run_dir / "workspace" / "answer.md").exists()
        print(
            f"{case.case_id}\t{args.route}\t{'有答案' if answered else '没有 answer.md'}\t{run_dir}"
        )
    return 1 if failed else 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    for case in store.all():
        d = store.dir(case.case_id)
        inputs = [
            name
            for name, ok in (
                ("a1a", (d / A1A_LISTENING).exists()),
                ("a1b", True),
                ("a2", (d / A2_EVIDENCE).exists()),
            )
            if ok
        ]
        runs = {
            r: len(list((d / "runs" / r).glob("*"))) if (d / "runs" / r).exists() else 0
            for r in codex_runner.ROUTES
        }
        run_text = " ".join(f"{r}={n}" for r, n in runs.items())
        print(
            f"{case.case_id}\t{case.source}\t{case.duration_s / 60:.1f} min\t"
            f"可跑:{','.join(inputs)}\t已跑:{run_text}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ear-baselines")
    parser.add_argument("--root", help="实验目录（默认 EAR_BASELINES_ROOT 或 ~/.ear-baselines）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="导入任意格式的录音文件或目录")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("octopus", help="从旧 Ear 导入已完整处理的 Octopus 录音（含 A2 证据）")
    p.add_argument("--capture", action="append", help="只导入这个 capture_id（可重复）")
    p.add_argument("--limit", type=int, help="最多导入几段")
    p.set_defaults(func=cmd_octopus)

    p = sub.add_parser("listen", help="A1a：用能听音频的模型生成听写记录")
    p.add_argument("case_ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--model", help="默认 EAR_BASELINES_AUDIO_MODEL 或 gpt-audio")
    p.add_argument("--chunk-seconds", type=float, help="默认 EAR_BASELINES_CHUNK_SECONDS 或 600")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("run", help="用 Codex 跑一条路线")
    p.add_argument("route", choices=codex_runner.ROUTES)
    p.add_argument("case_ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument(
        "--model",
        default=os.environ.get("EAR_BASELINES_CODEX_MODEL", codex_runner.DEFAULT_MODEL),
    )
    p.add_argument("--effort", default=codex_runner.DEFAULT_EFFORT)
    p.add_argument("--network", action="store_true", help="允许 Codex 联网（录音可能被外传）")
    p.add_argument("--timeout", type=float, default=3600.0, help="单次运行的秒数上限")
    p.add_argument("--task", help="换一个 TASK.md（默认 task_default.txt）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list", help="列出 case")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
