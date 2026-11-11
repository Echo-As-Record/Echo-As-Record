"""检查③是否已确定归属 payload 字段（⑤按钮入口依赖的唯一未定接口）。

只读：`git fetch origin` 后检查远端分支上的几个信号，不改仓库文件。

    uv run python services/dependency/scripts/check_attribution_fields.py [--no-fetch] [--json]

信号（任一出现即视为“③有进展，需要人工或 Codex 核对”）：
  registry_comment   origin/dev 登记表 $comment 不再写“待 ③ 负责人确认”
  attribution_schema contracts/ 下出现归属 payload 的 JSON Schema
  attribution_on_dev ③的代码（services/attribution/src/）合入 origin/dev
  p1_resolved        ③开发文档的 P1 行不再标“待确认”（查 origin/dev 和③分支）

退出码：0 = 有信号；1 = 仍在等待；2 = 检查本身出错（如 git 失败）。
输出的 fingerprint 是信号集合的摘要，调用方用它避免对同一状态重复处理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REMOTE = "origin"
DEV = f"{REMOTE}/dev"
ATTRIBUTION_BRANCH = f"{REMOTE}/attribution/initial-implementation"
REGISTRY = "contracts/registry/data-types.json"
PENDING_MARK = "待 ③ 负责人确认"
DEV_DOC = "services/attribution/ai-docs/development.md"


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


def ref_exists(ref: str, cwd: Path) -> bool:
    return bool(git("rev-parse", "--verify", "--quiet", ref, cwd=cwd, check=False).strip())


def show(ref: str, path: str, cwd: Path) -> str | None:
    if not ref_exists(ref, cwd):
        return None
    out = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
    )
    return out.stdout if out.returncode == 0 else None


def p1_row(doc: str | None) -> str | None:
    if doc is None:
        return None
    for line in doc.splitlines():
        if line.startswith("| P1 "):
            return line
    return None


def check(cwd: Path) -> dict:
    signals: dict[str, str] = {}

    registry = show(DEV, REGISTRY, cwd)
    if registry is None:
        raise GitError(f"{DEV}:{REGISTRY} 读不到")
    comment = json.loads(registry).get("$comment", "")
    if PENDING_MARK not in comment:
        signals["registry_comment"] = comment[:200]

    schemas = [
        p
        for p in git("ls-tree", "-r", "--name-only", DEV, "contracts/", cwd=cwd).splitlines()
        if "attribution" in Path(p).name and p.endswith(".schema.json")
    ]
    if schemas:
        signals["attribution_schema"] = ", ".join(schemas)

    src = git("ls-tree", "-r", "--name-only", DEV, "services/attribution/src/", cwd=cwd)
    if src.strip():
        signals["attribution_on_dev"] = f"{len(src.splitlines())} 个文件"

    for ref in (DEV, ATTRIBUTION_BRANCH):
        row = p1_row(show(ref, DEV_DOC, cwd))
        if row is not None and "待确认" not in row and "待③确认" not in row:
            signals[f"p1_resolved@{ref}"] = row[:200]

    heads = {
        ref: git("rev-parse", "--short", ref, cwd=cwd).strip()
        for ref in (DEV, ATTRIBUTION_BRANCH)
        if ref_exists(ref, cwd)
    }
    digest = hashlib.sha256(json.dumps(sorted(signals), ensure_ascii=False).encode()).hexdigest()
    return {
        "ready": bool(signals),
        "signals": signals,
        "fingerprint": digest[:16] if signals else None,
        "heads": heads,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查③是否已确定归属 payload 字段")
    parser.add_argument("--no-fetch", action="store_true", help="不执行 git fetch")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    cwd = Path(__file__).resolve().parent
    try:
        if not args.no_fetch:
            git("fetch", "--quiet", REMOTE, cwd=cwd)
        result = check(cwd)
    except (GitError, json.JSONDecodeError) as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["ready"]:
        print("③有进展，需要核对：")
        for name, detail in result["signals"].items():
            print(f"  - {name}: {detail}")
    else:
        heads = ", ".join(f"{k}={v}" for k, v in result["heads"].items())
        print(f"仍在等待③确定归属字段（{heads}）")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
