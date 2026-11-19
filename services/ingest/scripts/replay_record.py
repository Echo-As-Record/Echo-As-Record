"""Replay one stored phone record through the multipart endpoint."""

import argparse
import json
import os
from pathlib import Path

import httpx


def replay(metadata: Path, *, url: str, device: str, token: str) -> None:
    record = json.loads(metadata.read_text())
    (attachment,) = record.pop("files")
    record.pop("id")
    user = record.pop("user_id", None)
    fields = {
        key: value if isinstance(value, str) else json.dumps(value) for key, value in record.items()
    }
    blob = (metadata.parent / attachment["stored_name"]).resolve()
    if blob.parent != metadata.parent.resolve():
        raise ValueError("attachment must be in the metadata directory")
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device}
    if user:
        headers["X-User-Id"] = user
    with blob.open("rb") as source, httpx.Client(trust_env=False, timeout=60) as client:
        response = client.post(
            url.rstrip("/") + "/api/v1/record",
            data=fields,
            files={"file": (attachment["original_name"], source, attachment["content_type"])},
            headers=headers,
        )
    response.raise_for_status()
    print(f"{record['type']} HTTP {response.status_code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--url", required=True, help="ingest service root")
    parser.add_argument("--device", required=True, help="X-Device-Id stored in the inbox layout")
    args = parser.parse_args()
    replay(args.metadata, url=args.url, device=args.device, token=os.environ["EAR_INGEST_TOKEN"])
