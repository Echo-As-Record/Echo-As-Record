"""Prepare immutable exam materials, run isolated Codex containers, and compare answers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ear_baselines.cases import default_root, sha256_file, write_json
from ear_baselines.codex_runner import _usage

VERSION = "0.155.1"
IMAGE = f"ear-baselines-exam:{VERSION}"
PROMPT = (
    "回答 questions.md 里的问题。材料是一个人随身录音设备的数据，在 recordings/ 下："
    "每段录音一个目录，有原始音频（.m4a）、同时段的手机传感数据（context、imu，jsonl.gz）"
    "和 info.json（开始时间、时长）。录音里不只有说话，还有环境声音；定位和手机状态在传感数据里。\n"
    "你可以上网，可以下载和安装工具、模型；可以用环境变量 EARbaseline_key 调用 OpenAI 接口"
    "（地址见 EARbaseline_base_url，兼容官方 API；本次预算约 <预算> 美元，用完后接口会拒绝）。"
    "除此之外可以用一切办法，但只能在当前目录里工作。\n"
    "<开卷>"
    '把答案写到 answers.json：{"answers":[{"id":"B04","answer":"……",'
    '"evidence":[{"recording_id":"…","time":"HH:MM:SS"}],"confidence":"高|中|低"}]}；'
    "time 是当地时钟时刻（info.json 的开始时间加上在录音里的位置），不是录音内的偏移。"
    '录音里找不到依据的题写"无法回答"。中间产物（转写稿、脚本等）放在 work/ 下，结束时不要删除。'
)
PROXY_ENV = "EAR_BASELINES_EXAM_PROXY"
"""宿主代理在容器里的地址（colima 下如 http://192.168.5.2:7897）。本机只能经代理上外网。"""
OCTOPUS_PORT = 31415
"""Octopus 网关在宿主所有网卡上监听；自检时一并确认连不上。"""
OPEN_PROMPT = (
    "ear/ 下是另一套处理系统对这些录音的完整结果"
    "（转写、局部说话人标签、环境声、定位、手机状态），可以使用。\n"
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def safe(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]*", value):
        raise ValueError(f"非法名称：{value}")
    return value


def raw_files(audio: Path) -> list[Path]:
    if audio.suffix != ".m4a" or not audio.is_file():
        raise ValueError("inventory audio_path 必须是存在的原始 .m4a")
    prefix = audio.stem.split("__", 1)[0]
    files = [audio]
    for kind in ("context", "imu"):
        pattern = re.compile(rf"^{re.escape(prefix)}__(?:\d+__)?{kind}_.+\.jsonl\.gz$")
        matches = sorted(p for p in audio.parent.iterdir() if pattern.fullmatch(p.name))
        # Some exports attach sensors to the complete audio stem instead.
        matches += sorted(audio.parent.glob(f"{audio.stem}__{kind}_*.jsonl.gz"))
        if not matches and (end := re.search(r"_to_(\d\d-\d\d-\d\d)$", prefix)):
            # 手机偶尔把本段的 context 记在紧接着的短分段里（从本段结束时刻开始）。
            follow = re.compile(
                rf"^segment_\d+_from_{end[1]}_to_[\d-]+__(?:\d+__)?{kind}_.+\.jsonl\.gz$"
            )
            matches = sorted(p for p in audio.parent.iterdir() if follow.fullmatch(p.name))
        matches = sorted(set(matches))
        if not matches:
            raise FileNotFoundError(f"缺少同分段 {kind} 原始文件：{audio.name}")
        files.extend(matches)
    return files


def prepare(
    root: Path,
    name: str,
    questions: Path,
    ids: list[str],
    extra: list[str] | None = None,
    force: bool = False,
) -> Path:
    target = root / "exams" / safe(name)
    if target.exists() and not force:
        raise FileExistsError(target)
    source = read_json(questions)
    rows = source["questions"] if isinstance(source, dict) else source
    by_id = {q["id"]: q for q in rows}
    if not ids or len(ids) != len(set(ids)) or len(by_id) != len(rows):
        raise ValueError("题号不能为空或重复")
    selected = [by_id[i] for i in ids]
    recordings = sorted(
        {safe(e["recording_id"]) for q in selected for e in q["evidence"]}
        | {safe(i) for i in extra or []}
    )
    index = {r["id"]: r for r in read_json(root / "inventory/index.json")["recordings"]}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=target.parent) as temp:
        stage = Path(temp)
        material = stage / "material"
        (material / "ear").mkdir(parents=True)
        (material / "recordings").mkdir()
        for ident in recordings:
            row = index[ident]
            audio = Path(row["audio_path"]).expanduser()
            if not audio.is_absolute():
                audio = root / audio
            files = raw_files(audio)
            transcript = root / "inventory/transcripts" / f"{ident}.md"
            if not transcript.is_file():
                raise FileNotFoundError(transcript)
            dest = material / "recordings" / ident
            dest.mkdir()
            for path in files:
                if path.is_symlink():
                    raise ValueError("原始材料不接受符号链接")
                shutil.copyfile(path, dest / path.name)
            start = datetime.fromisoformat(row["started_at"])
            if start.tzinfo is None:
                raise ValueError("started_at 必须带时区")
            write_json(
                dest / "info.json",
                {
                    "recording_id": ident,
                    "started_at": start.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "duration_s": row["duration_s"],
                    "files": [p.name for p in files],
                },
            )
            shutil.copyfile(transcript, material / "ear" / f"{ident}.md")
        (material / "questions.md").write_text(
            "\n\n".join(f"## {safe(q['id'])}\n\n{q['question']}" for q in selected) + "\n", "utf-8"
        )
        write_json(
            stage / "answers_key.json",
            {
                "answers": [
                    {"id": q["id"], "answer": q["reference_answer"], "evidence": q["evidence"]}
                    for q in selected
                ]
            },
        )
        write_json(
            stage / "meta.json",
            {
                "question_ids": ids,
                "recording_ids": recordings,
                "generated_at": datetime.now(UTC).isoformat(),
                "questions_path": str(questions.resolve()),
                "questions_sha256": sha256_file(questions),
            },
        )
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(stage, target)
    return target


def workspace(exam: Path, group: str, budget: float) -> Path:
    if group not in ("open", "closed"):
        raise ValueError(group)
    run = exam / "runs" / group / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    work = run / "workspace"
    work.mkdir(parents=True)
    material = exam / "material"
    shutil.copyfile(material / "questions.md", work / "questions.md")
    for directory in ["recordings", *(["ear"] if group == "open" else [])]:
        source = material / directory
        if any(p.is_symlink() for p in source.rglob("*")) or source.is_symlink():
            raise ValueError("材料不接受符号链接")
        shutil.copytree(source, work / directory)
    prompt = PROMPT.replace("<预算>", str(budget)).replace(
        "<开卷>", OPEN_PROMPT if group == "open" else ""
    )
    (work / "PROMPT.md").write_text(prompt, "utf-8")
    return run


def proxy_env(proxy: str | None) -> list[str]:
    """容器经宿主代理上外网；meter 是同网络的容器，不走代理。"""
    if not proxy:
        return []
    env: list[str] = []
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env += ["-e", f"{key}={proxy}"]
    for key in ("NO_PROXY", "no_proxy"):
        env += ["-e", f"{key}=meter,localhost,127.0.0.1"]
    return env


def container(
    work: Path, home: Path, network: str, name: str, token: str, proxy: str | None = None
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--mount",
        f"type=bind,src={work.resolve()},dst=/work",
        "--mount",
        f"type=bind,src={home.resolve()},dst=/codex-home",
        "-e",
        "CODEX_HOME=/codex-home",
        "-e",
        f"EARbaseline_key={token}",
        "-e",
        "EARbaseline_base_url=http://meter:8787/v1",
        "-e",
        "TZ=Asia/Shanghai",
        *proxy_env(proxy),
        IMAGE,
    ]


def meter_command(
    log: Path,
    network: str,
    token: str,
    key: str,
    budget: float,
    name: str,
    proxy: str | None = None,
) -> list[str]:
    return [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        "meter",
        "--mount",
        f"type=bind,src={log.resolve()},dst=/meter.jsonl",
        "-e",
        f"EARbaseline_key={key}",
        "-e",
        f"METER_TOKEN={token}",
        "-e",
        f"METER_BUDGET={budget}",
        *proxy_env(proxy),
        IMAGE,
        "python3",
        "/opt/exam/meter.py",
    ]


def firewall_command(
    subnet: str, destination: str, delete: bool = False, port: int | None = None
) -> list[str]:
    """DOCKER-USER 规则：默认丢弃；给 port 时放行到该 TCP 端口（用于宿主代理）。"""
    allow = ["-p", "tcp", "--dport", str(port), "-j", "ACCEPT"] if port else ["-j", "DROP"]
    return [
        "colima",
        "ssh",
        "--",
        "sudo",
        "iptables",
        "-D" if delete else "-I",
        "DOCKER-USER",
        "-s",
        subnet,
        "-d",
        destination,
        *allow,
    ]


def host_addresses() -> list[str]:
    """宿主在局域网上的地址：容器能经 NAT 连到，需要和 192.168.5.0/24 一起挡住。"""
    found = []
    for interface in ("en0", "en1"):
        try:
            result = subprocess.run(
                ["ipconfig", "getifaddr", interface],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0 and result.stdout.strip():
            found.append(result.stdout.strip())
    return found


def redacted(command: list[str]) -> str:
    return shlex.join(
        [
            arg.split("=", 1)[0] + "=<redacted>"
            if arg.startswith(("EARbaseline_key=", "METER_TOKEN="))
            else arg
            for arg in command
        ]
    )


def invoke(
    command: list[str], *, check: bool = True, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    # Never print CalledProcessError: the proxy command contains the real key.
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"命令失败（{result.returncode}）：{redacted(command)}")
    return result


def validate_answers(path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("答案文件不接受符号链接")
    data = read_json(path)
    rows = data["answers"]
    if not isinstance(rows, list) or len(rows) != len(meta["question_ids"]):
        raise ValueError("答案题数不匹配")
    if {r["id"] for r in rows} != set(meta["question_ids"]):
        raise ValueError("答案题号不匹配")
    for row in rows:
        if not isinstance(row["answer"], str) or row["confidence"] not in ("高", "中", "低"):
            raise ValueError("答案或把握格式错误")
        if not isinstance(row["evidence"], list):
            raise ValueError("evidence 必须是列表")
        for evidence in row["evidence"]:
            if evidence["recording_id"] not in meta["recording_ids"] or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", evidence["time"]
            ):
                raise ValueError("证据录音或时间错误")
    return data


def run(
    exam: Path,
    group: str,
    *,
    budget: float = 5,
    model: str = "gpt-6-astra",
    effort: str = "medium",
    timeout_h: float = 4,
    dry_run: bool = False,
    firewall: bool = True,
    proxy: str | None = None,
) -> Path:
    if any(not math.isfinite(x) or x <= 0 for x in (budget, timeout_h)):
        raise ValueError("预算和超时必须是正数")
    out = workspace(exam, group, budget)
    work = out / "workspace"
    suffix = secrets.token_hex(6)
    network = f"ear-exam-{suffix}"
    names = [f"{network}-{role}" for role in ("codex", "check", "meter")]
    token = secrets.token_urlsafe(32)
    url = os.environ.get("EAR_ADAPTER_EAR_URL")
    port = (urlsplit(url).port or (443 if urlsplit(url).scheme == "https" else 80)) if url else None
    started = time.monotonic()
    summary: dict[str, Any] = {
        "group": group,
        "model": model,
        "effort": effort,
        "budget_usd": budget,
        "exit_code": None,
        "timed_out": False,
        "dry_run": dry_run,
        "isolation": None,
        "image_id": None,
    }
    proxy = proxy if proxy is not None else os.environ.get(PROXY_ENV) or None
    proxy_url = urlsplit(proxy) if proxy else None
    if proxy_url and not (proxy_url.hostname and proxy_url.port):
        raise ValueError(f"{PROXY_ENV} 需要形如 http://192.168.5.2:7897")
    summary["proxy"] = proxy
    summary["firewall"] = firewall
    rules: list[tuple[str, str, int | None]] = []
    network_attempted = False
    cleanup_errors = []
    (out / "meter.jsonl").touch()
    (out / "events.jsonl").touch()
    # colima 只共享主目录，系统临时目录挂不进容器；放在运行目录里（不在 /work 下），结束即删。
    with tempfile.TemporaryDirectory(prefix=".codex-home-", dir=out) as tmp:
        home = Path(tmp)
        base = container(work, home, network, names[0], token, proxy)
        prompt = (work / "PROMPT.md").read_text("utf-8")
        command = [
            *base,
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            "/work",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-o",
            "/work/.last_message.md",
            prompt,
        ]
        check_cmd = [
            *container(work, home, network, names[1], token, proxy),
            "python3",
            "/opt/exam/isolation.py",
            ",".join(str(p) for p in (port, OCTOPUS_PORT) if p),
            ",".join([] if dry_run else host_addresses()),
        ]
        meter_cmd = meter_command(
            out / "meter.jsonl",
            network,
            token,
            "<redacted>" if dry_run else os.environ.get("EARbaseline_key", ""),  # noqa: SIM112
            budget,
            names[2],
            proxy,
        )
        try:
            if dry_run:
                for cmd in (
                    ["docker", "network", "create", network],
                    meter_cmd,
                    check_cmd,
                    command,
                ):
                    print(redacted(cmd))
                if firewall:
                    print(redacted(firewall_command("<network-subnet>", "192.168.5.0/24")))
                    print(redacted(firewall_command("<network-subnet>", "<host-gateway>")))
                    print(redacted(firewall_command("<network-subnet>", "<host-lan-ip>")))
                    if proxy_url:
                        print(
                            redacted(
                                firewall_command(
                                    "<network-subnet>",
                                    str(proxy_url.hostname),
                                    port=proxy_url.port,
                                )
                            )
                        )
                for name in names:
                    print(shlex.join(["docker", "rm", "-f", name]))
                if firewall:
                    for destination in ("192.168.5.0/24", "<host-gateway>"):
                        print(redacted(firewall_command("<network-subnet>", destination, True)))
                print(shlex.join(["docker", "network", "rm", network]))
            else:
                if not os.environ.get("EARbaseline_key"):  # noqa: SIM112
                    raise ValueError("需要 EARbaseline_key")
                shutil.copyfile(Path.home() / ".codex/auth.json", home / "auth.json")
                (home / "auth.json").chmod(0o600)
                summary["image_id"] = invoke(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE]
                ).stdout.strip()
                network_attempted = True
                invoke(["docker", "network", "create", network])
                if firewall:
                    info = json.loads(invoke(["docker", "network", "inspect", network]).stdout)[0]
                    subnet = info["IPAM"]["Config"][0]["Subnet"]
                    gateway = info["IPAM"]["Config"][0]["Gateway"]
                    # Host-gateway can differ from this dedicated network's gateway.
                    bridge = json.loads(invoke(["docker", "network", "inspect", "bridge"]).stdout)[
                        0
                    ]
                    host_gateway = bridge["IPAM"]["Config"][0]["Gateway"]
                    drops = ["192.168.5.0/24", gateway, host_gateway, *host_addresses()]
                    for dest in dict.fromkeys(drops):
                        rules.append((subnet, dest, None))
                        invoke(firewall_command(subnet, dest))
                    if proxy_url:
                        # -I 插到最前：放行代理端口的规则要排在丢弃规则之前。
                        allow = (subnet, str(proxy_url.hostname), proxy_url.port)
                        rules.append(allow)
                        invoke(firewall_command(subnet, allow[1], port=allow[2]))
                invoke(meter_cmd)
                # Health is checked inside the proxy container; no host ports are published.
                health = [
                    "docker",
                    "exec",
                    names[2],
                    "python3",
                    "-c",
                    "import urllib.request; "
                    "urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2)",
                ]
                for _attempt in range(20):
                    if invoke(health, check=False).returncode == 0:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError("计量代理未就绪")
                checked = invoke(check_cmd, check=False, timeout=90)
                isolation = json.loads(checked.stdout)
                write_json(out / "isolation.json", isolation)
                summary["isolation"] = isolation
                if checked.returncode or not isolation.get("ok"):
                    raise RuntimeError("隔离自检失败，见 isolation.json")
                with (
                    (out / "events.jsonl").open("w") as events,
                    (out / "stderr.log").open("w") as stderr,
                ):
                    result = subprocess.run(
                        command,
                        stdout=events,
                        stderr=stderr,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout_h * 3600,
                        check=False,
                    )
                summary["exit_code"] = result.returncode
        except subprocess.TimeoutExpired:
            summary["timed_out"] = True
            summary["error"] = "容器命令超时"
        except (OSError, ValueError, RuntimeError) as exc:
            summary["error"] = str(exc)
        finally:
            if not dry_run and network_attempted:
                for name in names:
                    try:
                        if name == names[2]:
                            stopped = invoke(
                                ["docker", "stop", "--time", "130", name], check=False, timeout=140
                            )
                            if stopped.returncode:
                                summary["meter_incomplete"] = True
                            else:
                                state = invoke(
                                    ["docker", "inspect", "--format", "{{.State.ExitCode}}", name],
                                    check=False,
                                )
                                if state.returncode or state.stdout.strip() != "0":
                                    summary["meter_incomplete"] = True
                        result = invoke(["docker", "rm", "-f", name], check=False)
                        if result.returncode and "No such container" not in result.stderr:
                            cleanup_errors.append(f"container {name}")
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_errors.append(f"container {name}")
                for subnet, dest, allowed in reversed(rules):
                    try:
                        invoke(firewall_command(subnet, dest, True, allowed))
                    except (OSError, RuntimeError, subprocess.TimeoutExpired):
                        cleanup_errors.append(f"firewall {subnet} -> {dest}")
                try:
                    invoke(["docker", "network", "rm", network])
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    cleanup_errors.append(f"network {network}")
            summary["cleanup_errors"] = cleanup_errors
            summary["elapsed_s"] = round(time.monotonic() - started, 3)
            summary["isolation"] = summary["isolation"] or {
                "ok": False,
                "not_run": True,
                "dry_run": dry_run,
            }
            summary["usage"] = _usage(out / "events.jsonl")
            costs = []
            for line in (out / "meter.jsonl").read_text().splitlines():
                try:
                    costs.append(json.loads(line))
                except ValueError:
                    summary["meter_incomplete"] = True
            summary["api_estimated_usd"] = max((r["total_usd"] for r in costs), default=0)
            summary["api_uncertain"] = any(r.get("uncertain") for r in costs) or summary.get(
                "meter_incomplete", False
            )
            summary["over_budget_usd"] = max(0, summary["api_estimated_usd"] - budget)
            try:
                answers = validate_answers(work / "answers.json", read_json(exam / "meta.json"))
                write_json(out / "answers.json", answers)
                summary["answers_status"] = "valid"
            except (OSError, ValueError, TypeError, KeyError):
                summary["answers_status"] = "missing_or_invalid"
            if not (out / "isolation.json").exists():
                write_json(
                    out / "isolation.json", {"ok": False, "not_run": True, "dry_run": dry_run}
                )
            write_json(out / "run.json", summary)
    return out


def report(exam: Path) -> Path:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = [
        "# 考卷对照（未自动评分）",
        "",
        "| 组 | 耗时秒 | Codex token | API 美元（估算） | 状态 |",
        "|---|---|---|---|---|",
    ]
    answers = {}
    for group in ("open", "closed"):
        runs = sorted((exam / "runs" / group).glob("*/run.json"), reverse=True)
        latest = next((p for p in runs if not read_json(p).get("dry_run")), None)
        if latest:
            row = read_json(latest)
            lines.append(
                "| "
                + " | ".join(
                    cell(v)
                    for v in (
                        group,
                        row["elapsed_s"],
                        row["usage"],
                        row["api_estimated_usd"],
                        row.get("error", row["answers_status"]),
                    )
                )
                + " |"
            )
            path = latest.parent / "answers.json"
            answers[group] = (
                {r["id"]: r for r in read_json(path)["answers"]} if path.exists() else {}
            )
        else:
            answers[group] = {}
    lines += [
        "",
        "| 题号 | 参考答案及证据 | 开卷答案 / 把握 / 证据 | 闭卷答案 / 把握 / 证据 | 人工评分 |",
        "|---|---|---|---|---|",
    ]
    for key in read_json(exam / "answers_key.json")["answers"]:
        cells = [key["id"], cell({k: key[k] for k in ("answer", "evidence")})]
        cells += [cell(answers[g].get(key["id"], "缺失")) for g in ("open", "closed")]
        lines.append("| " + " | ".join(cells) + " | |")
    path = exam / "report.md"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def cli(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser() if args.root else default_root()
    try:
        if args.exam_command == "prepare":
            result = prepare(
                root,
                args.name,
                Path(args.questions),
                args.ids.split(","),
                args.extra_recordings.split(",") if args.extra_recordings else [],
                args.force,
            )
        elif args.exam_command == "report":
            result = report(root / "exams" / safe(args.name))
        else:
            result = run(
                root / "exams" / safe(args.name),
                args.group,
                budget=args.budget_usd,
                model=args.model,
                effort=args.effort,
                timeout_h=args.timeout_h,
                dry_run=args.dry_run,
                firewall=not args.no_firewall,
                proxy=args.proxy,
            )
            print(result)
            summary = read_json(result / "run.json")
            return int(
                not args.dry_run
                and (
                    summary.get("error") is not None
                    or summary["exit_code"] != 0
                    or summary["answers_status"] != "valid"
                    or bool(summary["cleanup_errors"])
                )
            )
        print(result)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"exam 失败：{exc}")
        return 1


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("exam", help="开卷 / 闭卷端到端问答")
    commands = parser.add_subparsers(dest="exam_command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("name")
    prepare_parser.add_argument("--questions", required=True)
    prepare_parser.add_argument("--ids", required=True)
    prepare_parser.add_argument("--extra-recordings")
    prepare_parser.add_argument("--force", action="store_true")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("group", choices=("open", "closed"))
    run_parser.add_argument("name")
    run_parser.add_argument("--budget-usd", type=float, default=5)
    run_parser.add_argument("--model", default="gpt-6-astra")
    run_parser.add_argument("--effort", default="medium")
    run_parser.add_argument("--timeout-h", type=float, default=4)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--no-firewall", action="store_true", help="不加 colima 防火墙（隔离自检多半会失败）"
    )
    run_parser.add_argument("--proxy", help=f"容器内可用的宿主代理，默认读 {PROXY_ENV}")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("name")
    parser.set_defaults(func=cli)
