"""Deterministic Chinese word segmentation and candidate expression counts."""

from __future__ import annotations

import re
from typing import Protocol

import jieba

_UNIT = re.compile(r"[A-Za-z0-9_']+|[一-鿿]")

FILLERS: tuple[str, ...] = ("就是", "然后", "那个", "其实", "我觉得", "对吧", "嗯", "啊", "呃")
"""口头禅候选。按子串在原文上计数，不依赖分词结果。"""


class Tokenizer(Protocol):
    identifier: str

    def tokens(self, text: str) -> list[str]: ...


class CharTokenizer:
    """拉丁词按词切（小写），汉字按单字切；词级信息由 n-gram 统计恢复。"""

    identifier = "char-v1"

    def tokens(self, text: str) -> list[str]:
        return [m.group(0).lower() for m in _UNIT.finditer(text)]


class ChineseTokenizer:
    """Pinned jieba dictionary/version, with HMM disabled for reproducibility."""

    identifier = "jieba-0.42.1-hmm-off"

    def tokens(self, text: str) -> list[str]:
        return [
            token.lower()
            for token in jieba.lcut(text, HMM=False)
            if any(c.isalnum() for c in token)
        ]


def count_fillers(text: str, fillers: tuple[str, ...] = FILLERS) -> dict[str, int]:
    """每个口头禅在文本中出现的次数，只返回出现过的。"""
    counts = {f: text.count(f) for f in fillers}
    return {f: n for f, n in counts.items() if n > 0}
