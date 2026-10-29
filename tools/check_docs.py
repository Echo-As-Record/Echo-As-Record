"""检查文档的审定状态。

规则（详见 AGENTS.md「文档分级」）：
- ai-docs/ 下（任意层级）是工作文档：未审定，不得带 reviewed-by。
- .github/ 下的模板不检查。
- 其余所有 Markdown 都是已审定文档：必须有 front matter，写明
  reviewed-by（审定人）和 reviewed-at（YYYY-MM-DD），且不能是 status: draft。
- 传入 --base <ref> 时（用于 PR），还检查：修改了已审定文档的正文，
  就必须同时更新 reviewed-by 或 reviewed-at。

用法：
    uv run python tools/check_docs.py
    uv run python tools/check_docs.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AI_DOCS_DIR = "ai-docs"
SKIPPED_PREFIXES = (".github/",)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str


def parse_front_matter(text: str) -> dict[str, str] | None:
    """解析最简单的 `key: value` front matter；没有 front matter 时返回 None。"""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.startswith((" ", "\t", "-", "#")):
            continue
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def classify(rel_path: str) -> str:
    """返回 'ai'、'skip' 或 'ratified'。"""
    if rel_path.startswith(SKIPPED_PREFIXES):
        return "skip"
    if AI_DOCS_DIR in Path(rel_path).parts:
        return "ai"
    return "ratified"


def list_markdown_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(p for p in result.stdout.splitlines() if (REPO_ROOT / p).is_file())


def check_file(rel_path: str, text: str) -> list[Problem]:
    kind = classify(rel_path)
    if kind == "skip":
        return []
    fm = parse_front_matter(text)
    if kind == "ai":
        if fm and fm.get("reviewed-by"):
            return [Problem(rel_path, "ai-docs/ 下的文档不应带 reviewed-by；审定后请移出 ai-docs/")]
        return []

    if fm is None:
        return [
            Problem(rel_path, "已审定范围内的文档缺少 front matter（reviewed-by / reviewed-at）")
        ]
    problems: list[Problem] = []
    if fm.get("status", "").lower() == "draft":
        problems.append(Problem(rel_path, "status: draft，待人完整审定（或移到 ai-docs/）"))
    if not fm.get("reviewed-by"):
        problems.append(Problem(rel_path, "缺少 reviewed-by"))
    reviewed_at = fm.get("reviewed-at", "")
    if not DATE_RE.match(reviewed_at):
        problems.append(Problem(rel_path, f"reviewed-at 应为 YYYY-MM-DD，当前为 {reviewed_at!r}"))
    return problems


def git_show(ref: str, rel_path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{rel_path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


def check_review_bumped(base: str) -> list[Problem]:
    """修改了已审定文档，就必须同时更新审定信息。"""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=M", f"{base}...HEAD", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    problems: list[Problem] = []
    for rel_path in result.stdout.splitlines():
        if classify(rel_path) != "ratified":
            continue
        old_text = git_show(base, rel_path)
        if old_text is None:
            continue
        new_text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        old_fm = parse_front_matter(old_text) or {}
        new_fm = parse_front_matter(new_text) or {}
        if all(old_fm.get(k) == new_fm.get(k) for k in ("reviewed-by", "reviewed-at")):
            problems.append(
                Problem(rel_path, "修改了已审定文档，但没有更新 reviewed-by / reviewed-at")
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查文档的审定状态")
    parser.add_argument("--base", help="PR 的目标分支，例如 origin/main")
    args = parser.parse_args(argv)

    problems: list[Problem] = []
    files = list_markdown_files()
    for rel_path in files:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        problems.extend(check_file(rel_path, text))
    if args.base:
        problems.extend(check_review_bumped(args.base))

    if problems:
        print(f"文档审定检查未通过（共 {len(problems)} 项）：")
        for p in problems:
            print(f"  - {p.path}: {p.message}")
        return 1
    print(f"文档审定检查通过（{len(files)} 个 Markdown 文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
