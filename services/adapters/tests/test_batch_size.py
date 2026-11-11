"""合成的 30 分钟数量级样本；只测 wire 字节数，不声称覆盖所有真实录音。"""

import json
import random

from ear_adapter import AdapterConfig, capture_records
from ear_adapter.testing import AUDIO_FILE, FakeSource, evidence_json
from ear_contracts import new_id
from ear_storage_client import AppendRaw, Context
from ear_storage_client.engine import canonical_json
from ear_storage_client.wire import encode_request


def test_typical_batch_size():
    source = FakeSource({"cap-1": evidence_json()})
    templates = {
        r.data_type: r
        for r in capture_records(
            source.read_capture("cap-1"),
            audio_file=AUDIO_FILE,
            embeddings={r: source.read_embedding(r) for r in ("run-1-res-1", "run-1-res-3")},
            config=AdapterConfig(),
        )
    }
    counts = dict(
        audio_ref=1,
        local_speaker=20,
        voiceprint=20,
        transcript_segment=400,
        ambient_sound=120,
        gps_point=2,
        phone_info=2,
    )
    rng = random.Random(42)
    ops = []
    for kind, count in counts.items():
        for i in range(count):
            record = templates[kind].model_copy(
                deep=True, update={"id": new_id(), "dedup_key": f"{kind}:{i}"}
            )
            if kind == "voiceprint":
                record.payload["embedding"]["values"] = [rng.uniform(-1, 1) for _ in range(192)]
                record.payload["embedding"]["shape"] = [192]
                record.payload["transcript_segment_ids"] = [new_id() for _ in range(20)]
            if kind == "transcript_segment":
                record.payload["text"] = "合成转写样本仅供容量测试" * 10
            ops.append(AppendRaw(record))
    body = json.loads(encode_request(ops, Context.system()))
    body["request_id"] = new_id()
    size = len(canonical_json(body).encode("utf-8"))
    print(f"synthetic batch: {len(ops)} operations, {size} bytes, {size / 2**20:.3f} MiB")
    assert len(ops) == 565
    assert size < 8 * 2**20
