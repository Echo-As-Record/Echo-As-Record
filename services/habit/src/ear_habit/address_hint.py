"""Conservative address-turn evidence for research, never an attribution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from ear_contracts import RawRecord, SemanticRecord
from ear_storage_client import Reader

from ear_habit.recognition_baseline import _at_time
from ear_habit.recognition_context import recognition_context


@dataclass(frozen=True, slots=True)
class AddressHint:
    person_id: str
    alias: str
    preceding_segment_id: str
    target_segment_id: str


def address_hints(
    reader: Reader,
    target_id: str,
    *,
    aliases: Mapping[str, str],
    as_of: datetime,
    max_gap: timedelta = timedelta(seconds=10),
) -> tuple[AddressHint, ...]:
    """Offer explicit alias-address candidates from the immediately preceding turn.

    Aliases are supplied by the evaluator. A reference and different local-speaker
    labels are required; the phrase can still address someone who does not reply.
    """
    if max_gap <= timedelta(0) or len(aliases) > 100:
        raise ValueError("invalid hint bounds")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str)
        for k, v in aliases.items()
    ):
        raise ValueError("invalid alias mapping")
    context = recognition_context(reader, target_id, lookback=max_gap, as_of=as_of)
    target = reader.get(target_id)
    if not isinstance(target, RawRecord) or not context.target_local_speaker_id:
        return ()
    previous = []
    for segment in context.preceding:
        source = reader.get(segment.segment_id)
        if isinstance(source, RawRecord):
            previous.append((source.event_end, source.id, source, segment))
    if not previous:
        return ()
    _, _, source, turn = max(previous, key=lambda row: (row[0], row[1]))
    if (
        not turn.local_speaker_id
        or turn.local_speaker_id == context.target_local_speaker_id
        or target.event_start - source.event_end > max_gap
    ):
        return ()
    hints = []
    for alias, person_id in sorted(aliases.items()):
        pattern = (
            r"(?:^|[，,。.!！？?\s])"
            + re.escape(alias)
            + r"[，,]\s*(?:你怎么看|你觉得呢|请说|你说)"
        )
        if not re.search(pattern, turn.text):
            continue
        person = reader.get(person_id)
        if not isinstance(person, SemanticRecord) or person.data_type != "person":
            raise ValueError("alias person unavailable")
        if _at_time(reader, person, as_of) is not None:
            hints.append(AddressHint(person_id, alias, source.id, target_id))
    return tuple(hints)
