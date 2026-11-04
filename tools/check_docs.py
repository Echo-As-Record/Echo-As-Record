"""检查 Markdown 受众、元数据及本地文件链接，不判断审定或自动授权状态迁移。

仅解析文首 --- 包围的单行 key: value（可用成对单双引号、独立注释行）；
不支持完整 YAML、多行值、集合或重复键。based-on 是自由文本，不解析为路径。

链接检查不是完整 Markdown parser：支持单行 [文字](路径)、![文字](路径)、
<路径> 形式的目标、单双引号 title、裸目标的一层配对括号及反斜杠转义标点。
忽略反引号 inline code、行首至多三个空格的反引号/波浪号 fenced code；
不解析引用式链接、HTML、缩进代码块或列表/引用容器内的 fence。
本地目标相对本文，解码百分号编码并忽略 query/fragment，只检查文件存在；
不联网、不检查标题 anchor；拒绝绝对路径及解析符号链接后越出仓库的路径。
.github 的 issue/PR Markdown 模板豁免，其他 Markdown 仍检查。

用法：uv run python tools/check_docs.py [--base origin/main]
--base 验证 Git ref，列出 base 到工作区的 Markdown 差异（含未追踪文件），
仅供人工 review；不要求修改 reviewed 字段，也不代表任何内容已获批准。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)
DOC_TYPES = {"design", "proposal", "decision", "reference", "guide", "status"}
STATUS_REQUIRED = {"design", "proposal", "decision"}
STATUSES = {"proposed", "accepted", "superseded"}
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.S)
LINK_RE = re.compile(
    r"(?<!\\)\[(?:\\.|[^\[\]\\\n])*\]\([ \t]*"
    r"(?P<target><[^<>\n]*>|(?:\\.|[^\s()<>\\]|\([^()\n]*\))*)"
    r"""(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'))?[ \t]*\)"""
)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str


def parse_front_matter(text: str) -> dict[str, str] | None:
    """返回简单单行元数据；缺失、未闭合或不支持的格式返回 None。"""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return None
    fields: dict[str, str] = {}
    for line in match[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9-]*", key) or key in fields:
            return None
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                return None
            value = value[1:-1].strip()
        elif value.startswith(("|", ">", "[", "{")):
            return None
        fields[key] = value
    return fields


def classify(rel_path: str) -> str:
    """目录只决定受众；任意层级 ai-docs 优先，根 AGENTS.md 也是 ai。"""
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] == ".github":
        template = parts[1].lower()
        if template in {"issue_template", "pull_request_template"} or (
            len(parts) == 2 and template in {"issue_template.md", "pull_request_template.md"}
        ):
            return "skip"
    if "ai-docs" in parts[:-1] or rel_path == "AGENTS.md":
        return "ai"
    return "human"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def list_markdown_files() -> list[str]:
    paths = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md")
    return sorted(
        p
        for p in set(paths.split("\0"))
        if p and ((REPO_ROOT / p).exists() or (REPO_ROOT / p).is_symlink())
    )


def target_problem(rel_path: str, target: str) -> str | None:
    """检查相对路径；先限制仓库边界，再检查文件，不读取目标内容。"""
    if Path(target).is_absolute() or SCHEME_RE.match(target):
        return "目标必须是仓库内的相对文件路径"
    try:
        resolved = (REPO_ROOT / rel_path).parent.joinpath(target).resolve()
        if not resolved.is_relative_to(REPO_ROOT.resolve()):
            return "目标越出仓库"
        if not resolved.is_file():
            return "目标文件不存在或不是文件"
    except (OSError, ValueError, RuntimeError):
        return "目标路径无效"
    return None


def without_code(text: str) -> str:
    """屏蔽受支持的 fence 和 inline code，保留换行及链接文字位置。"""
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = FENCE_RE.match(line.rstrip("\r\n"))
        if fence:
            if (
                match
                and match[1][0] == fence[0]
                and len(match[1]) >= len(fence)
                and not match[2].strip()
            ):
                fence = ""
            lines.append("\n")
        elif match and (match[1][0] != "`" or "`" not in match[2]):
            fence = match[1]
            lines.append("\n")
        else:
            lines.append(line)
    return INLINE_CODE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), "".join(lines))


def check_links(rel_path: str, text: str) -> list[Problem]:
    fm = FRONT_MATTER_RE.match(text)
    body = text[fm.end() :] if fm else text
    problems: list[Problem] = []
    for match in LINK_RE.finditer(without_code(body)):
        target = match["target"]
        if target.startswith("<"):
            target = target[1:-1]
        target = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", target)
        if not target or target.startswith(("#", "//")) or SCHEME_RE.match(target):
            continue
        path = unquote(re.split(r"[?#]", target, maxsplit=1)[0])
        if not path:  # query-only 链接仍指向本文
            continue
        error = target_problem(rel_path, path)
        if error:
            problems.append(Problem(rel_path, f"链接 {target!r}: {error}"))
    return problems


def check_file(rel_path: str, text: str) -> list[Problem]:
    kind = classify(rel_path)
    if kind == "skip":
        return []
    problems = check_links(rel_path, text)
    fm = parse_front_matter(text)
    if fm is None:
        return [Problem(rel_path, "缺少或无效的单行 front matter"), *problems]
    if fm.get("audience") not in {"human", "ai"}:
        problems.append(Problem(rel_path, "audience 必须为 human 或 ai"))
    elif fm["audience"] != kind:
        problems.append(Problem(rel_path, f"此路径的 audience 必须为 {kind}"))
    if fm.get("doc-type") not in DOC_TYPES:
        problems.append(Problem(rel_path, "doc-type 必须为 " + "|".join(sorted(DOC_TYPES))))
    if (fm.get("doc-type") in STATUS_REQUIRED or "status" in fm) and fm.get(
        "status"
    ) not in STATUSES:
        problems.append(Problem(rel_path, "status 必须为 proposed|accepted|superseded"))
    if fm.get("status") == "superseded":
        target = fm.get("superseded-by", "")
        error = target_problem(rel_path, target) if target else "必须非空"
        if error:
            problems.append(Problem(rel_path, f"superseded-by: {error}"))
    if fm.get("audience") == "ai" and not fm.get("based-on"):
        problems.append(Problem(rel_path, "ai audience 必须有非空 based-on（自由文本溯源）"))
    if "reviewed-by" in fm or "reviewed-at" in fm:
        if not fm.get("reviewed-by") or not fm.get("reviewed-at"):
            problems.append(Problem(rel_path, "reviewed-by / reviewed-at 必须成对且非空"))
        reviewed_at = fm.get("reviewed-at", "")
        if reviewed_at:
            try:
                if not DATE_RE.fullmatch(reviewed_at):
                    raise ValueError
                date.fromisoformat(reviewed_at)
            except ValueError:
                problems.append(Problem(rel_path, "reviewed-at 必须为真实 ISO 日期 YYYY-MM-DD"))
    return problems


def changed_markdown_files(base: str) -> list[str]:
    """与 base 本身比较（不是 merge-base），包含暂存、未暂存、删除和未追踪文件。"""
    commit = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    changed = git("diff", "--no-renames", "--name-only", "-z", commit, "--", "*.md")
    untracked = git("ls-files", "-z", "--others", "--exclude-standard", "--", "*.md")
    return sorted(
        p for p in set(changed.split("\0") + untracked.split("\0")) if p and classify(p) != "skip"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查文档受众、元数据和本地链接（不代表审定）")
    parser.add_argument(
        "--base", help="验证目标 ref 并提示相对工作区的 Markdown 差异，仅供人工 review"
    )
    args = parser.parse_args(argv)
    problems: list[Problem] = []
    try:
        if args.base:
            changed = changed_markdown_files(args.base)
            print(f"人工 review 提示（相对 {args.base}，含未追踪文件；不代表批准状态迁移）：")
            for path in changed:
                print(f"  - {path}")
        files = [p for p in list_markdown_files() if classify(p) != "skip"]
        for rel_path in files:
            error = target_problem(rel_path, Path(rel_path).name)
            if error:
                problems.append(Problem(rel_path, error))
                continue
            try:
                text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                problems.append(Problem(rel_path, f"无法读取 Markdown: {exc}"))
                continue
            problems.extend(check_file(rel_path, text))
    except subprocess.CalledProcessError as exc:
        print(f"Git 检查失败（请检查 --base ref 及仓库）：{exc.stderr.strip()}", file=sys.stderr)
        return 1
    if problems:
        print(f"文档检查未通过（共 {len(problems)} 项）：")
        for problem in problems:
            print(f"  - {problem.path}: {problem.message}")
        return 1
    print(f"文档检查通过（{len(files)} 个 Markdown 文件；不代表人工审定）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
