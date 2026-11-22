"""Bounded transcript lookup for the API, with references resolved under one identity."""

from typing import Any

from ear_contracts import RawRecord, SemanticRecord, is_id
from ear_storage_client import Query, TimeRange
from ear_storage_client.protocol import Reader


def candidate(reader: Reader, segment: RawRecord, text: str, position: int) -> dict[str, Any]:
    data = segment.payload
    voice = data.get("voiceprint_id")
    audio_id = data.get("audio_ref_id")
    excerpt_start = max(0, position - 80)
    item = {
        "transcript_segment_id": segment.id,
        "start": segment.event_start.isoformat().replace("+00:00", "Z"),
        "end": segment.event_end.isoformat().replace("+00:00", "Z"),
        "text": text[excerpt_start : excerpt_start + 400],
        "local_speaker_id": data.get("local_speaker_id"),
        "voiceprint_id": voice,
        "audio_ref_id": audio_id,
        "playback": None,
        "attribution": None,
    }
    audio = reader.get(audio_id) if isinstance(audio_id, str) and is_id(audio_id) else None
    if isinstance(audio, RawRecord) and audio.data_type == "audio_ref":
        item["playback"] = {
            "file_ref": audio.payload.get("file_ref"),
            "start_seconds": (segment.event_start - audio.event_start).total_seconds(),
            "end_seconds": (segment.event_end - audio.event_start).total_seconds(),
        }
    if is_id(voice):
        matches = reader.query(Query("attribution", where={"subject_id": voice}, limit=2))
        if len(matches.items) == 1 and matches.next_cursor is None:
            attr = matches.items[0]
            if isinstance(attr, SemanticRecord) and attr.payload.get("target_type") == "person":
                pid = attr.payload.get("target_id")
                person = reader.get(pid) if isinstance(pid, str) and is_id(pid) else None
                if isinstance(person, SemanticRecord) and person.data_type == "person":
                    item["attribution"] = {
                        "id": attr.id,
                        "version": attr.version,
                        "person_id": pid,
                        "name": person.payload.get("name"),
                    }
    return item


def search(
    reader: Reader, time: TimeRange, phrase: str, cursor: str | None, limit: int
) -> dict[str, Any]:
    items = []
    # At most 100 segments per call. Only advance past rows actually examined.
    scanned = 0
    while len(items) < limit and scanned < 100:
        page = reader.query(
            Query("transcript_segment", time=time, cursor=cursor, limit=min(25, 100 - scanned))
        )
        for index, segment in enumerate(page.items):
            scanned += 1
            text = segment.payload.get("text")
            if isinstance(segment, RawRecord) and isinstance(text, str):
                position = text.find(phrase)
                if position >= 0:
                    items.append(candidate(reader, segment, text, position))
            if len(items) == limit and index + 1 < len(page.items):
                # Ask storage for its opaque cursor at the precise resume position.
                cursor = reader.query(
                    Query("transcript_segment", time=time, cursor=cursor, limit=index + 1)
                ).next_cursor
                return {"items": items, "next_cursor": cursor}
        cursor = page.next_cursor
        if not page.items or cursor is None:
            break
    return {"items": items, "next_cursor": cursor}
