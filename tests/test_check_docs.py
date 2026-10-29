"""tools/check_docs.py 的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_docs

RATIFIED = "---\nreviewed-by: aytony\nreviewed-at: 2026-10-29\n---\n\n# 标题\n"


def test_parse_front_matter() -> None:
    fm = check_docs.parse_front_matter(RATIFIED)
    assert fm == {"reviewed-by": "aytony", "reviewed-at": "2026-10-29"}
    assert check_docs.parse_front_matter("# 没有 front matter\n") is None


def test_classify() -> None:
    assert check_docs.classify("ai-docs/proposals/glossary.md") == "ai"
    assert check_docs.classify("services/storage/ai-docs/notes.md") == "ai"
    assert check_docs.classify(".github/pull_request_template.md") == "skip"
    assert check_docs.classify("docs/design/design-v1.1.md") == "ratified"
    assert check_docs.classify("README.md") == "ratified"


def test_ratified_doc_passes() -> None:
    assert check_docs.check_file("docs/x.md", RATIFIED) == []


def test_ratified_doc_without_front_matter_fails() -> None:
    assert len(check_docs.check_file("docs/x.md", "# x\n")) == 1


def test_draft_fails() -> None:
    text = "---\nstatus: draft\nreviewed-by:\nreviewed-at:\n---\n# x\n"
    messages = [p.message for p in check_docs.check_file("README.md", text)]
    assert len(messages) == 3


def test_ai_doc_must_not_claim_review() -> None:
    assert check_docs.check_file("ai-docs/x.md", RATIFIED) != []
    assert check_docs.check_file("ai-docs/x.md", "# 随手笔记\n") == []
