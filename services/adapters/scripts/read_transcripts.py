"""Read final archive transcripts for one device and an explicit wall-clock interval."""

import argparse
import json
import os
from datetime import UTC, datetime

from ear_contracts.records import RawRecord
from ear_storage_client import Query, TimeRange
from ear_storage_client.remote import RemoteStorageClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--device", required=True)
    args = parser.parse_args()
    if not args.start.tzinfo or not args.end.tzinfo or args.end <= args.start:
        parser.error("Use ISO timestamps with a UTC offset and end after start")
    window = TimeRange(args.start.astimezone(UTC), args.end.astimezone(UTC))
    cursor = None
    with RemoteStorageClient(
        os.environ["EAR_ADAPTER_STORAGE_TOKEN"], os.environ["EAR_ADAPTER_STORAGE_URL"]
    ) as client:
        while True:
            page = client.query(Query("transcript_segment", time=window, cursor=cursor))
            rows = [
                row
                for row in page.items
                if isinstance(row, RawRecord) and row.source.device == args.device
            ]
            speakers = (
                client.get_many(list({row.payload["local_speaker_id"] for row in rows}))
                if rows
                else {}
            )
            for row in rows:
                speaker = speakers.get(row.payload["local_speaker_id"])
                print(
                    json.dumps(
                        {
                            "id": row.id,
                            "audio_ref_id": row.payload["audio_ref_id"],
                            "local_speaker_id": row.payload["local_speaker_id"],
                            "start": row.event_start.astimezone(args.start.tzinfo).isoformat(),
                            "end": row.event_end.astimezone(args.start.tzinfo).isoformat(),
                            "speaker": speaker.payload["label"] if speaker else None,
                            "speaker_scope": row.payload["upstream"]["audio_chunk_id"],
                            "text": row.payload["text"],
                        },
                        ensure_ascii=False,
                    )
                )
            cursor = page.next_cursor
            if not cursor:
                break


if __name__ == "__main__":
    main()
