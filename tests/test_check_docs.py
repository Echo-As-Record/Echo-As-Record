"""文档受众、单行元数据、本地链接及 Git CLI 行为；Git 集成测试只用临时仓库。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_docs


def document(audience: str = "human", doc_type: str = "guide", extra: str = "") -> str:
    return f"---\naudience: {audience}\ndoc-type: {doc_type}\n{extra}---\n\n# 标题\n"


def messages(path: str, text: str) -> str:
    return "\n".join(p.message for p in check_docs.check_file(path, text))


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(check_docs, "REPO_ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(document(), encoding="utf-8")
    (tmp_path / "docs/new.md").write_text(document(), encoding="utf-8")
    return tmp_path


def test_parse_front_matter() -> None:
    assert check_docs.parse_front_matter(document()) == {"audience": "human", "doc-type": "guide"}
    assert check_docs.parse_front_matter(
        "---\r\n# 注释\r\naudience: 'human'\r\ndoc-type: \"guide\"\r\n"
        "based-on: 会议纪要: 未保存为文件\r\n---"
    ) == {"audience": "human", "doc-type": "guide", "based-on": "会议纪要: 未保存为文件"}
    assert check_docs.parse_front_matter("---\n---\n") == {}


@pytest.mark.parametrize(
    "text",
    [
        "# no front matter",
        "---\naudience: human\n",
        "\n---\naudience: human\n---\n",
        "---\naudience: human\naudience: ai\n---\n",
        "---\nbased-on: |\n  body\n---\n",
        "---\naudience: 'human\n---\n",
        "---\nnot a field\n---\n",
    ],
)
def test_invalid_front_matter(text: str) -> None:
    assert check_docs.parse_front_matter(text) is None
    assert "front matter" in messages("README.md", text)


@pytest.mark.parametrize(
    ("path", "audience"),
    [
        ("ai-docs/proposals/x.md", "ai"),
        ("services/storage/ai-docs/x.md", "ai"),
        ("docs/ai-docs/x.md", "ai"),
        ("AGENTS.md", "ai"),
        ("docs/design/x.md", "human"),
        ("README.md", "human"),
        ("services/storage/README.md", "human"),
        ("services/storage/AGENTS.md", "human"),
        ("not-ai-docs/x.md", "human"),
        (".github/README.md", "human"),
    ],
)
def test_path_audience(path: str, audience: str) -> None:
    assert check_docs.classify(path) == audience
    extra = "based-on: 讨论结果（非路径）\n" if audience == "ai" else ""
    assert check_docs.check_file(path, document(audience, extra=extra)) == []
    wrong = "human" if audience == "ai" else "ai"
    assert f"必须为 {audience}" in messages(path, document(wrong, extra=extra))


@pytest.mark.parametrize(
    "path",
    [
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug.md",
        ".github/PULL_REQUEST_TEMPLATE/default.md",
        ".github/issue_template.md",
    ],
)
def test_github_templates_exempt(path: str) -> None:
    assert check_docs.classify(path) == "skip"
    assert check_docs.check_file(path, "[bad](../../missing.md)") == []


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ("", "audience"),
        ("audience: HUMAN\n", "audience"),
        ("audience: human\n", "doc-type"),
        ("audience: human\ndoc-type: draft\n", "doc-type"),
    ],
)
def test_required_metadata(extra: str, expected: str) -> None:
    assert expected in messages("README.md", f"---\n{extra}---\n")


@pytest.mark.parametrize("doc_type", sorted(check_docs.DOC_TYPES))
@pytest.mark.parametrize("status", [None, "proposed", "accepted", "draft", "", "ACCEPTED"])
def test_doc_types_and_status(doc_type: str, status: str | None) -> None:
    extra = "" if status is None else f"status: {status}\n"
    result = check_docs.check_file("docs/x.md", document(doc_type=doc_type, extra=extra))
    valid = status in {"proposed", "accepted"} or (
        status is None and doc_type not in check_docs.STATUS_REQUIRED
    )
    assert (result == []) == valid
    if not valid:
        assert all("status" in p.message for p in result)


@pytest.mark.parametrize("path", ["ai-docs/x.md", "services/m/ai-docs/x.md", "AGENTS.md"])
@pytest.mark.parametrize("based_on", ["", 'based-on: ""\n', "based-on:   \n"])
def test_ai_requires_nonempty_based_on(path: str, based_on: str) -> None:
    assert "based-on" in messages(path, document("ai", extra=based_on))


def test_ai_proposal_and_free_text_provenance() -> None:
    text = document("ai", "proposal", "status: proposed\nbased-on: 讨论: ../不存在.md + 人工说明\n")
    assert check_docs.check_file("ai-docs/x.md", text) == []


@pytest.mark.parametrize("doc_type", sorted(check_docs.DOC_TYPES))
def test_superseded_relative_file(root: Path, doc_type: str) -> None:
    text = document(doc_type=doc_type, extra="status: superseded\nsuperseded-by: new.md\n")
    assert check_docs.check_file("docs/old.md", text) == []
    assert check_docs.check_file("docs/old.md", text.replace("new.md", "../README.md")) == []


@pytest.mark.parametrize(
    "target",
    ["", '""', "missing.md", "..", "../../outside.md", "/etc/passwd", "https://example.org/new.md"],
)
def test_superseded_invalid_target(root: Path, target: str) -> None:
    text = document(extra=f"status: superseded\nsuperseded-by: {target}\n")
    assert "superseded-by" in messages("docs/old.md", text)


def test_superseded_requires_field() -> None:
    assert "superseded-by" in messages("README.md", document(extra="status: superseded\n"))


@pytest.mark.parametrize(("path", "audience"), [("docs/x.md", "human"), ("ai-docs/x.md", "ai")])
def test_review_optional_and_allowed_everywhere(path: str, audience: str) -> None:
    base = "based-on: 测试溯源\n" if audience == "ai" else ""
    for review in ("", "reviewed-by: test-reviewer\nreviewed-at: 2024-02-29\n"):
        assert check_docs.check_file(path, document(audience, extra=base + review)) == []


@pytest.mark.parametrize(
    "extra",
    [
        "reviewed-by: test-reviewer\n",
        "reviewed-at: 2024-02-29\n",
        "reviewed-by:\nreviewed-at:\n",
        "reviewed-by: ''\nreviewed-at: 2024-02-29\n",
        "reviewed-by: test-reviewer\nreviewed-at: ''\n",
    ],
)
def test_review_pair_must_be_nonempty(extra: str) -> None:
    assert "成对且非空" in messages("README.md", document(extra=extra))


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-29",
        "2024-02-30",
        "2024-13-01",
        "0000-01-01",
        "20240229",
        "2024-2-29",
        "2024-02-29T00:00:00Z",
    ],
)
def test_review_date_is_real_iso(value: str) -> None:
    text = document(extra=f"reviewed-by: test-reviewer\nreviewed-at: {value}\n")
    assert "真实 ISO 日期" in messages("README.md", text)


@pytest.mark.parametrize(
    "body",
    [
        "[new](new.md)",
        "[root](../README.md#no-such-heading)",
        "[new](new.md?raw=1#anything)",
        '[new](new.md "Title")',
        "[new](<new.md> 'Title')",
        "![image](new.md)",
        "[label `code`](new.md)",
        "[中文](new.md)",
    ],
)
def test_existing_local_links(root: Path, body: str) -> None:
    assert check_docs.check_file("docs/old.md", document() + body) == []


@pytest.mark.parametrize(
    "body",
    [
        "[x](https://example.invalid/x.md)",
        "[x](http://example.invalid)",
        "[x](mailto:test@example.invalid)",
        "[x](ssh://example.invalid/x)",
        "[x](//example.invalid/x)",
        "[x](#missing-anchor)",
        "[x]()",
        "[x](?raw=1)",
    ],
)
def test_external_and_same_page_links_ignored(body: str) -> None:
    assert check_docs.check_file("README.md", document() + body) == []


@pytest.mark.parametrize(
    "body",
    [
        "[x](missing.md)",
        "![image](missing.png)",
        "[x](<missing file.md>)",
        "[label `code`](missing.md)",
        "[x](missing.md 'title')",
        "[x](.)",
    ],
)
def test_missing_local_links(root: Path, body: str) -> None:
    assert "链接" in messages("docs/old.md", document() + body)


def test_link_paths_with_spaces_parentheses_and_escapes(root: Path) -> None:
    (root / "docs/a b(1).md").write_text("target", encoding="utf-8")
    (root / "docs/a(1).md").write_text("target", encoding="utf-8")
    body = (
        "[x](<a b(1).md>) [x](a%20b%281%29.md) [x](a(1).md) "
        r"[x](a\(1\).md)"
    )
    assert check_docs.check_file("docs/old.md", document() + body) == []


@pytest.mark.parametrize(
    "body",
    [
        "`[fake](missing.md)`",
        "``[fake](missing.md) ` nested``",
        "`multi\n[fake](missing.md)\nline`",
        "```md\n[fake](missing.md)\n```\n",
        "~~~md\n[fake](missing.md)\n~~~\n",
        "   ```md\n[fake](missing.md)\n   ```\n",
        "````md\n```\n[fake](missing.md)\n````\n",
        "```md\n~~~\n[fake](missing.md)\n```\n",
        "```md\n[fake](missing.md)\n",
        r"\[literal](missing.md)",
        "[x][ref]\n\n[ref]: missing.md",
    ],
)
def test_code_examples_and_unsupported_reference_links_ignored(body: str) -> None:
    assert check_docs.check_file("README.md", document() + body) == []


@pytest.mark.parametrize(
    "body",
    [
        "`code` [real](missing.md)",
        "```\n[fake](fake.md)\n```\n[real](missing.md)",
        "~~~\n[fake](fake.md)\n~~~~\n[real](missing.md)",
        "````\n[fake](fake.md)\n````\n[real](missing.md)",
    ],
)
def test_real_links_after_code_still_checked(body: str) -> None:
    result = check_docs.check_file("README.md", document() + body)
    assert len(result) == 1
    assert "missing.md" in result[0].message


@pytest.mark.parametrize(
    "target",
    [
        "../../outside.md",
        "%2e%2e/%2e%2e/outside.md",
        "/etc/passwd",
        "%2Fetc/passwd",
        "bad%00name.md",
    ],
)
def test_link_path_boundaries(root: Path, target: str) -> None:
    assert "链接" in messages("docs/old.md", document() + f"[x]({target})")


def test_symlinks_cannot_escape_repository(
    root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "docs/escape.md").symlink_to(outside)
    assert "越出仓库" in messages("docs/old.md", document() + "[x](escape.md)")
    text = document(extra="status: superseded\nsuperseded-by: escape.md\n")
    assert "越出仓库" in messages("docs/old.md", text)
    (root / "docs/inside.md").symlink_to(root / "README.md")
    assert check_docs.check_file("docs/old.md", document() + "[x](inside.md)") == []


def test_front_matter_provenance_is_not_a_markdown_link() -> None:
    text = document("ai", extra="based-on: 讨论 [记录](not-a-file.md)\n")
    assert check_docs.check_file("AGENTS.md", text) == []


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(root: Path) -> Path:
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Test Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "test fixture")
    return root


def test_untracked_markdown_checked_and_ignored_files_excluded(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (repo / "ignored.md").write_text("not checked", encoding="utf-8")
    path = repo / "新 文件.md"
    path.write_text("missing metadata", encoding="utf-8")
    assert "新 文件.md" in check_docs.list_markdown_files()
    assert "ignored.md" not in check_docs.list_markdown_files()
    assert check_docs.main([]) == 1
    assert "新 文件.md" in capsys.readouterr().out
    path.write_text(document(), encoding="utf-8")
    git(repo, "add", "新 文件.md")
    path.write_text(document() + "modified", encoding="utf-8")
    assert check_docs.list_markdown_files().count("新 文件.md") == 1
    assert check_docs.main([]) == 0


def test_base_diff_includes_worktree_staged_deleted_and_untracked(repo: Path) -> None:
    (repo / "README.md").write_text(document() + "unstaged", encoding="utf-8")
    (repo / "staged.md").write_text(document(), encoding="utf-8")
    git(repo, "add", "staged.md")
    (repo / "untracked.md").write_text(document(), encoding="utf-8")
    (repo / "docs/new.md").unlink()
    assert check_docs.changed_markdown_files("HEAD") == [
        "README.md",
        "docs/new.md",
        "staged.md",
        "untracked.md",
    ]
    assert "docs/new.md" not in check_docs.list_markdown_files()
    assert check_docs.main(["--base", "HEAD"]) == 0


@pytest.mark.parametrize("audience", ["human", "ai"])
def test_base_does_not_require_review_fields_to_change(
    repo: Path, audience: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "README.md" if audience == "human" else "AGENTS.md"
    extra = "reviewed-by: test-reviewer\nreviewed-at: 2024-02-29\n"
    if audience == "ai":
        extra += "based-on: 讨论结果\n"
    text = document(audience, "proposal", extra + "status: proposed\n")
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "--quiet", "-m", "review fixture")
    (repo / path).write_text(text.replace("proposed", "accepted") + "new body", encoding="utf-8")
    assert check_docs.main(["--base", "HEAD"]) == 0
    output = capsys.readouterr().out
    assert path in output
    assert "人工 review" in output
    assert "不代表批准状态迁移" in output


@pytest.mark.parametrize("ref", ["missing-ref", "--help", "HEAD:README.md"])
def test_base_invalid_ref_is_clean_failure(
    repo: Path, ref: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_docs.main([f"--base={ref}"]) == 1
    assert "Git 检查失败" in capsys.readouterr().err


def test_main_rejects_external_markdown_symlink_before_reading(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path_factory.mktemp("external") / "secret.md"
    outside.write_text(document(), encoding="utf-8")
    (repo / "external.md").symlink_to(outside)
    assert check_docs.main([]) == 1
    assert "越出仓库" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["dangling-link", "invalid-utf8"])
def test_unreadable_markdown_is_reported(
    repo: Path, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / "broken.md"
    if kind == "dangling-link":
        path.symlink_to(repo / "missing.md")
    else:
        path.write_bytes(b"\xff")
    assert "broken.md" in check_docs.list_markdown_files()
    assert check_docs.main([]) == 1
    assert "broken.md" in capsys.readouterr().out


def test_fence_with_trailing_text_does_not_close_code() -> None:
    body = "```md\n```not-a-closing-fence\n[fake](missing.md)\n```\n"
    assert check_docs.check_file("README.md", document() + body) == []
