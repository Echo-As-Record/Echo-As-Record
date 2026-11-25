"""L1：桶级统计。所有量都是整数计数，因此可加可减。

用户把一段话从甲纠正为乙时，只需要在两个桶上做减法和加法，不必重扫历史。
桶里存计数而不是比率，比率在读取时再算。
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ear_habit.tokenize import ChineseTokenizer, Tokenizer, count_fillers

SCHEMA = 2
SCALAR_FIELDS = ("segments", "chars", "tokens", "duration_ms")
COUNTER_FIELDS = ("unigrams", "bigrams", "fillers")

Stats = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Utterance:
    """一段转写：文本和它的时长（毫秒，整数，避免浮点累加漂移）。"""

    text: str
    duration_ms: int = 0


def empty_stats(tokenizer_id: str = ChineseTokenizer.identifier) -> Stats:
    return {
        "schema": SCHEMA,
        "tokenizer": tokenizer_id,
        **{f: 0 for f in SCALAR_FIELDS},
        **{f: {} for f in COUNTER_FIELDS},
    }


def utterance_stats(utt: Utterance, tokenizer: Tokenizer | None = None) -> Stats:
    tokenizer = tokenizer or ChineseTokenizer()
    tokens = tokenizer.tokens(utt.text)
    bigrams = Counter(" ".join(pair) for pair in itertools.pairwise(tokens))
    return {
        "schema": SCHEMA,
        "tokenizer": tokenizer.identifier,
        "segments": 1,
        "chars": len(utt.text),
        "tokens": len(tokens),
        "duration_ms": utt.duration_ms,
        "unigrams": dict(Counter(tokens)),
        "bigrams": dict(bigrams),
        "fillers": count_fillers(utt.text),
    }


def stats_of(utterances: Iterable[Utterance], tokenizer: Tokenizer | None = None) -> Stats:
    tokenizer = tokenizer or ChineseTokenizer()
    total = empty_stats(tokenizer.identifier)
    for utt in utterances:
        total = merge(total, utterance_stats(utt, tokenizer))
    return total


def _combine(a: Stats, b: Stats, sign: int) -> Stats:
    if (
        a.get("schema") != SCHEMA
        or b.get("schema") != SCHEMA
        or a.get("tokenizer") != b.get("tokenizer")
    ):
        raise ValueError("incompatible statistics schema or tokenizer")
    out: Stats = {"schema": SCHEMA, "tokenizer": a["tokenizer"]}
    for name in SCALAR_FIELDS:
        value = a[name] + sign * b[name]
        if value < 0:
            raise ValueError(f"{name} 减成负数：{value}")
        out[name] = value
    for name in COUNTER_FIELDS:
        merged = dict(a[name])
        for key, count in b[name].items():
            value = merged.get(key, 0) + sign * count
            if value < 0:
                raise ValueError(f"{name}[{key!r}] 减成负数：{value}")
            if value:
                merged[key] = value
            else:
                merged.pop(key, None)
        out[name] = merged
    return out


def merge(a: Stats, b: Stats) -> Stats:
    return _combine(a, b, 1)


def subtract(a: Stats, b: Stats) -> Stats:
    """从 a 里去掉 b。b 的内容必须全部包含在 a 里，否则报 ValueError。"""
    return _combine(a, b, -1)


def speech_rate(stats: Stats) -> float | None:
    """每秒字数；没有时长时返回 None。读取时计算，不存储。"""
    if stats["duration_ms"] <= 0:
        return None
    return stats["chars"] * 1000 / stats["duration_ms"]


def top(stats: Stats, name: str, n: int = 20) -> list[tuple[str, int]]:
    """词频靠前的 n 项，同频按键排序，保证结果稳定。"""
    items = stats[name].items()
    return sorted(items, key=lambda kv: (-kv[1], kv[0]))[:n]
