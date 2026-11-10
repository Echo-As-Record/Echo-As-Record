"""用同一个目标（TASK.md）在三种输入上运行 Codex，保存答案和运行记录。

每次运行一个独立工作区：

    <case>/runs/<route>/<UTC 时间>/
        workspace/TASK.md         目标（默认 task_default.txt，可用 --task 换）
        workspace/input/...       该路线的输入
        workspace/answer.md       Codex 写的结果
        events.jsonl              codex exec --json 的事件流（含工具调用，用来看 A1b 怎么处理音频）
        last_message.md           Codex 最后一条消息
        run.json                  路线、模型、参数、耗时、退出码

路线：
- a1a：input/listening.md（openai_audio 生成的听写记录）
- a1b：input/recording.m4a（原始音频，16 kHz 单声道）；Codex 自己决定怎么处理
- a2 ：input/evidence.md（旧 Ear Evidence 时间线）

为减少变量，运行时不加载用户的 Codex config 和 rules（--ignore-user-config、--ignore-rules），
模型和推理强度显式传入；沙箱默认禁止联网。
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ear_baselines.cases import A1A_LISTENING, A2_EVIDENCE, AUDIO_NAME, Case, CaseStore, write_json

Route = Literal["a1a", "a1b", "a2"]
ROUTES: tuple[Route, ...] = ("a1a", "a1b", "a2")
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "medium"

PROMPT = {
    "a1a": "阅读 TASK.md 并完成。材料是 input/listening.md：另一个模型直接听录音写下的听写记录。",
    "a1b": (
        "阅读 TASK.md 并完成。材料是 input/recording.m4a 这段原始录音，没有任何现成的文字稿；"
        "怎么从录音里得到需要的信息由你决定（本机可以使用已安装的命令行工具）。"
    ),
    "a2": (
        "阅读 TASK.md 并完成。材料是 input/evidence.md："
        "一套语音处理管线从录音和手机传感数据里整理出的时间线。"
    ),
}


@dataclass(frozen=True, slots=True)
class RunOptions:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    network: bool = False
    """允许 Codex 在沙箱里联网（A1b 想自己装 ASR 工具时需要）。真实录音可能因此被上传到外部。"""
    timeout_s: float = 3600.0
    task: Path | None = None


def default_task() -> str:
    resource = importlib.resources.files("ear_baselines").joinpath("task_default.txt")
    return resource.read_text("utf-8")


def _input_files(store: CaseStore, case: Case, route: Route) -> dict[str, Path]:
    d = store.dir(case.case_id)
    source = {
        "a1a": ("listening.md", d / A1A_LISTENING),
        "a1b": ("recording.m4a", d / AUDIO_NAME),
        "a2": ("evidence.md", d / A2_EVIDENCE),
    }[route]
    name, path = source
    if not path.exists():
        hint = {
            "a1a": "先运行 ear-baselines listen",
            "a1b": "先导入录音",
            "a2": "只有 Octopus 录音有，先运行 ear-baselines octopus",
        }[route]
        raise FileNotFoundError(f"{case.case_id} 缺少 {path.name}（{hint}）")
    return {name: path}


def codex_command(workspace: Path, route: Route, options: RunOptions, last: Path) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-s",
        "workspace-write",
        "-C",
        str(workspace),
        "-m",
        options.model,
        "-c",
        f'model_reasoning_effort="{options.effort}"',
        "-o",
        str(last),
    ]
    if options.network:
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    cmd.append(PROMPT[route])
    return cmd


def prepare(store: CaseStore, case: Case, route: Route, options: RunOptions) -> Path:
    """建好一次运行的目录和工作区，返回运行目录。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = store.dir(case.case_id) / "runs" / route / stamp
    workspace = run_dir / "workspace"
    (workspace / "input").mkdir(parents=True)
    task = options.task.read_text("utf-8") if options.task else default_task()
    (workspace / "TASK.md").write_text(task, encoding="utf-8")
    for name, path in _input_files(store, case, route).items():
        shutil.copy2(path, workspace / "input" / name)
    return run_dir


def run(store: CaseStore, case: Case, route: Route, options: RunOptions) -> Path:
    """运行一次 Codex，返回运行目录。Codex 失败也保留目录和 run.json，便于对比。"""
    if shutil.which("codex") is None:
        raise RuntimeError("找不到 codex 命令行")
    run_dir = prepare(store, case, route, options)
    workspace = run_dir / "workspace"
    last = run_dir / "last_message.md"
    cmd = codex_command(workspace, route, options, last)
    started = time.monotonic()
    timed_out = False
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as events:
        try:
            proc = subprocess.run(
                cmd,
                stdout=events,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=options.timeout_s,
                check=False,
                env={**os.environ},
            )
            code, stderr = proc.returncode, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out, code = True, None
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    (run_dir / "stderr.log").write_text(stderr or "", encoding="utf-8")
    write_json(
        run_dir / "run.json",
        {
            "case_id": case.case_id,
            "route": route,
            "model": options.model,
            "effort": options.effort,
            "network": options.network,
            "task": str(options.task) if options.task else "task_default.txt",
            "command": [*cmd[:-1], "<prompt>"],
            "prompt": PROMPT[route],
            "exit_code": code,
            "timed_out": timed_out,
            "elapsed_s": round(time.monotonic() - started, 1),
            "answer_written": (workspace / "answer.md").exists(),
            "codex_home_agents_md": (Path.home() / ".codex" / "AGENTS.md").exists(),
            "usage": _usage(run_dir / "events.jsonl"),
        },
    )
    return run_dir


def _usage(events: Path) -> object:
    """从事件流里取最后一次 token 用量（没有就返回 None）。"""
    found: object = None
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and "usage" in event:
            found = event["usage"]
    return found
