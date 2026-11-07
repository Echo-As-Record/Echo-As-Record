from __future__ import annotations

import json

import pytest
from ear_contracts import new_id
from ear_storage.codec import decode_result, encode_result, fingerprint
from ear_storage.schema import module_schema
from ear_storage_client import Context, Create, InvalidRequest, WriteResult
from ear_storage_client.results import Conflict, OpResult
from pydantic import ValidationError


def test_result_codec_conflict_tuples_and_unknown_outcomes() -> None:
    result = WriteResult(
        None, conflict=Conflict(0, "stale_dependency", "stale", details={"fields": ("x",)})
    )
    restored = decode_result(encode_result(result))
    assert restored.conflict is not None
    # JSON details is deliberately JSON-shaped; no opaque Python tuple markers.
    assert restored == result
    good = WriteResult(
        new_id(), results=(OpResult(0, "written", "activity", new_id(), 1, None, None),)
    )
    assert decode_result(encode_result(good)) == good
    data = json.loads(encode_result(good))
    data["result"]["results"][0]["outcome"] = "future"
    with pytest.raises(ValidationError):
        decode_result(json.dumps(data))
    with pytest.raises(InvalidRequest):
        decode_result('{"codec":2,"result":{}}')


def test_fingerprint_types_context_and_key_order() -> None:
    id = new_id()
    a = [Create("activity", id, {"a": 1, "b": True})]
    b = [Create("activity", id, {"b": True, "a": 1})]
    assert fingerprint(a, Context.system()) == fingerprint(b, Context.system())
    assert fingerprint(a, Context.system()) != fingerprint(
        [Create("activity", id, {"a": 1.0, "b": True})], Context.system()
    )
    assert fingerprint(a, Context.system()) != fingerprint(a, Context.system().deeper())


@pytest.mark.parametrize("value", ["evil;DROP SCHEMA storage", "a" * 56, "storage.x", "", "A", "é"])
def test_safe_module_schema(value: str) -> None:
    with pytest.raises(ValueError):
        module_schema(value)


def test_all_operation_codec_roundtrips_without_numeric_coercion() -> None:
    from ear_contracts import Cause, VersionRef
    from ear_storage.codec import decode_request, encode_request
    from ear_storage_client import (
        AppendRaw,
        DependencyInput,
        MarkProcessed,
        PublishReevaluation,
        PutSystemRecord,
        RegisterDependencies,
        Supersede,
        Update,
    )
    from ear_storage_client.testing import make_raw

    id = new_id()
    ref = VersionRef(data_type="activity", id=id, version=1)
    ops = [
        AppendRaw(make_raw("voiceprint", {"n": 1.0}, start="2026-10-30T00:00:00Z")),
        Create("activity", id, {"n": 1, "float": 1.0, "bool": True}),
        Update("activity", id, 1, {"x": None}),
        Supersede("activity", id, 2),
        RegisterDependencies("activity", id, [ref, DependencyInput(ref, ("x",))]),
        PutSystemRecord("extraction_run", new_id(), {"finished": True}),
        MarkProcessed("extraction", new_id()),
        PublishReevaluation(
            "activity",
            id,
            Cause(data_type="activity", data_id=id, old_version=None, new_version=1),
            "changed",
            new_id(),
        ),
    ]
    text = encode_request(ops, Context.system())
    decoded, context = decode_request(text)
    assert context == Context.system()
    assert encode_request(decoded, context) == text
    assert type(decoded[1].payload["n"]) is int  # type: ignore[union-attr]
    assert type(decoded[1].payload["float"]) is float  # type: ignore[union-attr]
    assert type(decoded[1].payload["bool"]) is bool  # type: ignore[union-attr]
    bad = json.loads(text)
    bad["ops"][1]["body"]["expected_version"] = True
    bad["ops"][1]["op"] = "Update"
    with pytest.raises(InvalidRequest):
        decode_request(json.dumps(bad))


@pytest.mark.parametrize(
    "text", ["[]", "null", '{"codec":true}', '{"codec":1,"context":{},"ops":[null]}']
)
def test_malformed_request_envelopes_are_invalid_requests(text: str) -> None:
    from ear_storage.codec import decode_request

    with pytest.raises(InvalidRequest):
        decode_request(text)
