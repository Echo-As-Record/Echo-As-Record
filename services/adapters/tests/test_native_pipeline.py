from __future__ import annotations

import hashlib
import io
import json
import wave
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from unittest.mock import Mock

import httpx
import pytest
from ear_adapter.config import AdapterConfig
from ear_adapter.convert import AmbientEvent, FileRef
from ear_adapter.ear_api import AudioChunk, Embedding, SpeechSegment
from ear_adapter.native_channels import (
    CaptureInput,
    ChannelInputError,
    OctopusInboxChannel,
    SourceChannel,
)
from ear_adapter.native_pipeline import (
    NATIVE_ADAPTER_VERSION,
    ASRResult,
    SpeakerResult,
    ingest_native,
    native_audio_ref_id,
)
from ear_adapter.recording_intake import RecordingIntake, RecordingJob
from ear_adapter.recording_worker import NativeRecordingPreparation, NativeRecordingPublication
from ear_adapter.runtime import AlreadyIngested, Failed, Ingested, Pending, ingest_capture
from ear_adapter.testing import FakeLocator, FakeSource, evidence_json
from ear_contracts import DataChanged, RawRecord, format_utc
from ear_storage_client import MemoryStorage, Query
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown, ResourceLimit
from ear_storage_client.wire import decode_request, encode_result


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(16000))
    return buffer.getvalue()


WAV = _wav()
START = datetime(2026, 5, 13, tzinfo=UTC)
END = START + timedelta(seconds=1)
SHA = hashlib.sha256(WAV).hexdigest()
FIXTURE_CONFIG = AdapterConfig(channel="fixture_channel")
EMBEDDING = Embedding(
    model_version="synthetic-speaker/1", shape=(2,), dtype="float32", values=[1, 2]
)


@dataclass(frozen=True)
class _SecondChannel:
    """Fixture-only descriptor for an unrelated non-inbox layout."""

    root: Path
    channel_id = "fixture_channel"

    def load(self, record_locator: str) -> CaptureInput:
        assert record_locator == "nested/demo.wav"
        return CaptureInput(
            "cap-1",
            "phone-01",
            "rec-1",
            "group-1",
            START,
            END,
            "audio/wav",
            FileRef(record_locator, SHA, len(WAV)),
        )


@pytest.fixture
def capture(tmp_path: Path) -> CaptureInput:
    channel: SourceChannel = _SecondChannel(tmp_path)
    locator = "nested/demo.wav"
    (tmp_path / locator).parent.mkdir()
    (tmp_path / locator).write_bytes(WAV)
    return channel.load(locator)


def _octopus(root: Path) -> tuple[SourceChannel, CaptureInput]:
    channel: SourceChannel = OctopusInboxChannel(root)
    directory = root / "inbox/brain/phone-01/2026-05-13/phone/00-00-00__group-1"
    directory.mkdir(parents=True)
    name = "segment_0_from_00-00-00_to_00-00-01__01__demo.wav"
    (directory / name).write_bytes(WAV)
    locator = directory / "segment_0_richVoice_from_00-00-00_to_00-00-01.json"
    locator.write_text(
        json.dumps(
            {
                "id": "rec-1",
                "type": "richVoice",
                "group_id": "group-1",
                "timestamp": "2026-05-13T00:00:00Z",
                "segment_index": "0",
                "segment_start_ms": "0",
                "segment_end_ms": "1000",
                "files": [
                    {
                        "field_name": "file",
                        "stored_name": name,
                        "size": len(WAV),
                        "sha256": SHA,
                        "content_type": "audio/wav",
                    }
                ],
            }
        )
    )
    return channel, channel.load(locator.relative_to(root).as_posix())


def _segment(label: str = "A", ident: str = "seg-1") -> SpeechSegment:
    return SpeechSegment(
        speech_segment_id=ident,
        audio_chunk_id="chunk-1",
        diarization_scope_id="scope-1",
        local_speaker_id=label,
        started_at=START,
        ended_at=END,
        original_text="synthetic text",
        overlap=False,
        language="en",
        model_version="synthetic-asr/1",
    )


CHUNK = AudioChunk(audio_chunk_id="chunk-1", started_at=START, ended_at=END, model_version="synth")
RESOLUTION = SpeakerResult("r1", "scope-1", "A", ("seg-1",), EMBEDDING)
EVENT = AmbientEvent("amb-1", START, END, "synthetic sound", "test", "synthetic-ambient/1")


@dataclass
class _FakeProvider:
    asr_result: ASRResult = field(
        default_factory=lambda: ASRResult("complete", (CHUNK,), (_segment(),))
    )
    speakers_result: tuple[SpeakerResult, ...] = (RESOLUTION,)
    ambient_result: tuple[AmbientEvent, ...] = (EVENT,)
    fail_at: str | None = None
    calls: list[str] = field(default_factory=list)
    stream: BinaryIO | None = None

    def _read(self, stage: str, audio: BinaryIO) -> None:
        self.calls.append(stage)
        if self.stream is None:
            self.stream = audio
        assert audio is self.stream
        assert audio.tell() == 0 and audio.read() == WAV
        if self.fail_at == stage:
            raise RuntimeError("private stub provider diagnostic")

    def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult:
        self._read("asr", audio)
        return self.asr_result

    def speakers(
        self, audio: BinaryIO, capture: CaptureInput, asr: ASRResult
    ) -> tuple[SpeakerResult, ...]:
        self._read("speaker", audio)
        assert asr.status == "complete" and asr.segments
        return self.speakers_result

    def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]:
        self._read("ambient", audio)
        return self.ambient_result


def _run(
    tmp_path: Path,
    capture: CaptureInput,
    storage: MemoryStorage,
    provider: _FakeProvider,
    *,
    config: AdapterConfig = FIXTURE_CONFIG,
):
    return ingest_native(
        capture, root=tmp_path, storage=storage.client("adapter"), provider=provider, config=config
    )


def test_channel_links_and_batch(tmp_path: Path, capture: CaptureInput) -> None:
    storage = MemoryStorage()
    client = storage.client("adapter")
    octopus, octopus_capture = _octopus(tmp_path)
    fixture = _SecondChannel(tmp_path)
    for channel, item in ((octopus, octopus_capture), (fixture, capture)):
        cfg = AdapterConfig(channel=channel.channel_id)
        provider = _FakeProvider()
        outcome = _run(tmp_path, item, storage, provider, config=cfg)
        assert isinstance(outcome, Ingested)
        assert outcome.deduplicated == 0 and sum(outcome.written.values()) == 5
        assert provider.calls == ["asr", "speaker", "ambient"]
        assert provider.stream is not None and provider.stream.closed
        events = [e for e in storage.events[-5:] if isinstance(e, DataChanged)]
        assert len(events) == 5 and len({e.batch_id for e in events}) == 1
        records = {}
        for evt in events:
            rec = client.get(evt.data_id)
            assert isinstance(rec, RawRecord) and rec.source.channel == channel.channel_id
            assert (
                rec.source.device == item.device_id
                and rec.adapter_version == NATIVE_ADAPTER_VERSION
            )
            records[rec.data_type] = rec
        audio = records["audio_ref"]
        assert audio.id == native_audio_ref_id(item, cfg)
        assert audio.payload["file_ref"] == item.file_ref.to_json()
        assert audio.payload["upstream"] == {
            "service": "ear-native",
            "capture_id": item.capture_id,
            "record_id": item.record_id,
            "group_id": item.group_id,
        }
        for typ in ("voiceprint", "transcript_segment", "local_speaker", "ambient_sound"):
            assert records[typ].payload["audio_ref_id"] == audio.id
        voice, transcript, speaker = (
            records[t] for t in ("voiceprint", "transcript_segment", "local_speaker")
        )
        assert voice.payload["transcript_segment_ids"] == [transcript.id]
        assert transcript.payload["voiceprint_id"] == voice.id
        assert (
            voice.payload["local_speaker_id"]
            == transcript.payload["local_speaker_id"]
            == speaker.id
        )
        never = _FakeProvider(fail_at="asr")
        assert isinstance(_run(tmp_path, item, storage, never, config=cfg), AlreadyIngested)
        assert never.calls == []
    assert len(storage.events) == 10


def test_distinct_from_legacy_and_profile_changes(tmp_path: Path, capture: CaptureInput) -> None:
    raw = evidence_json()
    old_capture = next(
        item["payload"] for item in raw["evidence"] if item["ref"]["type"] == "capture"
    )
    old_capture["audio_ref"]["checksum"] = f"sha256:{SHA}"
    old_capture["instance_id"], old_capture["record_id"] = capture.device_id, capture.record_id
    old_capture["started_at"], old_capture["ended_at"] = START.isoformat(), END.isoformat()
    storage = MemoryStorage()
    client = storage.client("adapter")
    assert isinstance(
        ingest_capture(
            "cap-1",
            source=FakeSource({"cap-1": raw}),
            storage=client,
            locator=FakeLocator(),
            config=AdapterConfig(),
        ),
        Ingested,
    )
    [old] = client.query(Query("audio_ref")).items
    config = AdapterConfig()
    native_id = native_audio_ref_id(capture, config)
    assert native_id != old.id
    assert isinstance(_run(tmp_path, capture, storage, _FakeProvider(), config=config), Ingested)
    profile = replace(config, adapter_version=f"{NATIVE_ADAPTER_VERSION}+new-settings")
    assert native_audio_ref_id(capture, profile) != native_id
    assert isinstance(_run(tmp_path, capture, storage, _FakeProvider(), config=profile), Ingested)
    assert client.count(Query("audio_ref")) == 3 and client.get(old.id) == old
    assert native_audio_ref_id(capture, replace(config, channel="fixture_channel")) != native_id
    different_bytes = WAV[:-2] + b"\x01\x00"
    (tmp_path / capture.file_ref.path).write_bytes(different_bytes)
    new_capture = replace(
        capture,
        file_ref=replace(capture.file_ref, sha256=hashlib.sha256(different_bytes).hexdigest()),
    )
    assert native_audio_ref_id(new_capture, config) != native_id
    invalid = replace(config, adapter_version="ear-audio/99.0.0")
    provider = _FakeProvider()
    assert isinstance(_run(tmp_path, new_capture, storage, provider, config=invalid), Failed)
    assert not provider.calls


@pytest.mark.parametrize("events", [(), (EVENT,)])
def test_valid_empty_asr(
    tmp_path: Path, capture: CaptureInput, events: tuple[AmbientEvent, ...]
) -> None:
    provider = _FakeProvider(asr_result=ASRResult("complete"), ambient_result=events)
    storage = MemoryStorage()
    outcome = _run(tmp_path, capture, storage, provider)
    assert isinstance(outcome, Ingested)
    expected = {"audio_ref": 1, **({"ambient_sound": 1} if events else {})}
    assert dict(outcome.written) == expected
    assert provider.calls == ["asr", "ambient"] and len(storage.events) == len(expected)


def test_speech_with_no_usable_vector(tmp_path: Path, capture: CaptureInput) -> None:
    provider = _FakeProvider(speakers_result=(replace(RESOLUTION, embedding=None),))
    storage = MemoryStorage()
    outcome = _run(tmp_path, capture, storage, provider)
    assert isinstance(outcome, Ingested) and "voiceprint" not in outcome.written
    [transcript] = storage.client("adapter").query(Query("transcript_segment")).items
    assert transcript.payload["voiceprint_id"] is None


@pytest.mark.parametrize(
    "stage,calls",
    [
        ("asr", ["asr"]),
        ("speaker", ["asr", "speaker"]),
        ("ambient", ["asr", "speaker", "ambient"]),
    ],
)
def test_exception_is_sanitized_and_never_writes(
    tmp_path: Path, capture: CaptureInput, stage, calls
) -> None:
    provider = _FakeProvider(fail_at=stage)
    storage = MemoryStorage()
    outcome = _run(tmp_path, capture, storage, provider)
    assert isinstance(outcome, Failed) and "private" not in outcome.reason
    assert provider.calls == calls and not storage.events


@pytest.mark.parametrize(
    "partial", [ASRResult("partial"), ASRResult("partial", (CHUNK,), (_segment(),))]
)
def test_partial_is_not_empty(tmp_path: Path, capture: CaptureInput, partial: ASRResult) -> None:
    provider = _FakeProvider(asr_result=partial)
    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
    assert provider.calls == ["asr"] and not storage.events


@pytest.mark.parametrize(
    "change",
    [
        {"speech_segment_id": "bad:id"},
        {"audio_chunk_id": "other-chunk"},
        {"started_at": END},
        {"ended_at": END + timedelta(microseconds=1)},
        {"started_at": START.replace(tzinfo=None)},
        {"local_speaker_id": "."},
    ],
)
def test_bad_asr_range_reference_or_id(tmp_path: Path, capture: CaptureInput, change: dict) -> None:
    segment = _segment().model_copy(update=change)
    provider = _FakeProvider(asr_result=ASRResult("complete", (CHUNK,), (segment,)))
    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
    assert provider.calls == ["asr"] and not storage.events


@pytest.mark.parametrize(
    "provider",
    [
        _FakeProvider(asr_result=ASRResult("complete", (CHUNK, CHUNK), (_segment(),))),
        _FakeProvider(asr_result=ASRResult("complete", (CHUNK,), (_segment(), _segment()))),
        _FakeProvider(speakers_result=()),
        _FakeProvider(speakers_result=(RESOLUTION, RESOLUTION)),
        _FakeProvider(speakers_result=(RESOLUTION, replace(RESOLUTION, resolution_id="r2"))),
        _FakeProvider(speakers_result=(replace(RESOLUTION, evidence_segment_ids=()),)),
        _FakeProvider(
            speakers_result=(replace(RESOLUTION, evidence_segment_ids=("seg-1", "seg-1")),)
        ),
        _FakeProvider(speakers_result=(replace(RESOLUTION, evidence_segment_ids=("unknown",)),)),
        _FakeProvider(speakers_result=(replace(RESOLUTION, sample_quality=float("nan")),)),
        _FakeProvider(ambient_result=(EVENT, EVENT)),
        _FakeProvider(ambient_result=(replace(EVENT, started_at=END),)),
    ],
)
def test_malformed_grouping_or_ambient_never_writes(
    tmp_path: Path, capture: CaptureInput, provider: _FakeProvider
) -> None:
    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
    assert not storage.events


@pytest.mark.parametrize(
    "change",
    [
        {"values": [float("nan"), 1]},
        {"values": [1, float("inf")]},
        {"values": [-0.0, 0.0]},
        {"shape": (1,)},
        {"shape": (1, 2)},
        {"values": [True, 1]},
        {"provenance": {"private": float("nan")}},
    ],
)
def test_invalid_vector(tmp_path: Path, capture: CaptureInput, change: dict) -> None:
    embedding = EMBEDDING.model_copy(update=change)
    provider = _FakeProvider(speakers_result=(replace(RESOLUTION, embedding=embedding),))
    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
    assert not storage.events


def test_cross_speaker_refs_and_inconsistent_vector_dimensions(
    tmp_path: Path, capture: CaptureInput
) -> None:
    asr = ASRResult("complete", (CHUNK,), (_segment(), _segment("B", "seg-2")))
    second = SpeakerResult("r2", "scope-1", "B", ("seg-2",), EMBEDDING)
    storage = MemoryStorage()
    embedding = EMBEDDING.model_copy(update={"shape": (3,), "values": [1, 2, 3]})
    for wrong in (
        replace(second, evidence_segment_ids=("seg-1",)),
        replace(second, embedding=embedding),
    ):
        provider = _FakeProvider(asr_result=asr, speakers_result=(RESOLUTION, wrong))
        assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
        assert not storage.events


@pytest.mark.parametrize(
    "fields", [{"record_id": "../x"}, {"device_id": "x\0y"}, {"group_id": "a:b"}]
)
def test_malformed_source_descriptor_rejected(capture: CaptureInput, fields: dict) -> None:
    with pytest.raises(ChannelInputError, match=r"^invalid_input$"):
        replace(capture, **fields)


@pytest.mark.parametrize("replace_name", [True, False])
def test_mutation_of_pinned_bytes_or_name_never_writes(
    tmp_path: Path, capture: CaptureInput, replace_name: bool
) -> None:
    class Mutator(_FakeProvider):
        def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]:
            events = super().ambient(audio, capture)
            path = tmp_path / capture.file_ref.path
            if replace_name:
                other = path.with_name("replacement.wav")
                other.write_bytes(WAV)
                other.replace(path)  # Same bytes, different inode.
            else:
                path.write_bytes(WAV[:-2] + b"\x01\x00")
            return events

    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, Mutator()), Failed)
    assert not storage.events


def test_preexisting_mutation_rejected_before_provider(
    tmp_path: Path, capture: CaptureInput
) -> None:
    path = tmp_path / capture.file_ref.path
    path.write_bytes(WAV[:-2] + b"\x01\x00")
    storage, provider = MemoryStorage(), _FakeProvider()
    assert isinstance(_run(tmp_path, capture, storage, provider), Failed)
    assert not provider.calls and not storage.events


def test_identical_speaker_labels_in_distinct_scopes_do_not_merge(
    tmp_path: Path, capture: CaptureInput
) -> None:
    other = _segment(ident="seg-2").model_copy(
        update={
            "diarization_scope_id": "scope-2",
            "audio_chunk_id": "chunk-2",
        }
    )
    other_chunk = CHUNK.model_copy(update={"audio_chunk_id": "chunk-2"})
    provider = _FakeProvider(
        asr_result=ASRResult("complete", (CHUNK, other_chunk), (_segment(), other)),
        speakers_result=(RESOLUTION, SpeakerResult("r2", "scope-2", "A", ("seg-2",), EMBEDDING)),
    )
    storage = MemoryStorage()
    assert isinstance(_run(tmp_path, capture, storage, provider), Ingested)
    client = storage.client("adapter")
    names = client.query(Query("local_speaker")).items
    transcripts = client.query(Query("transcript_segment")).items
    assert len(names) == len(transcripts) == 2
    assert {t.payload["local_speaker_id"] for t in transcripts} == {speaker.id for speaker in names}
    assert len({t.payload["voiceprint_id"] for t in transcripts}) == 2


def test_get_race_revalidated_before_write(tmp_path: Path, capture: CaptureInput) -> None:
    remote = Mock(spec=RemoteStorageClient)
    path = tmp_path / capture.file_ref.path

    def read(_id: str):
        if remote.get.call_count == 2:
            path.write_bytes(WAV[:-2] + b"\x01\x00")
        return None

    remote.get.side_effect = read
    outcome = ingest_native(
        capture, root=tmp_path, storage=remote, provider=_FakeProvider(), config=AdapterConfig()
    )
    assert isinstance(outcome, Failed)
    remote.write.assert_not_called()


@pytest.mark.parametrize("mutate", [True, False])
def test_uncertain_commit_replays_only_verified_input(
    tmp_path: Path, capture: CaptureInput, monkeypatch, mutate: bool
) -> None:
    storage = MemoryStorage()
    remote = Mock(spec=RemoteStorageClient)
    remote.get.side_effect = storage.client("adapter").get
    calls = []

    def write(ops, context, *, request_id):
        calls.append((ops, context, request_id))
        if len(calls) == 1:
            if mutate:
                (tmp_path / capture.file_ref.path).write_bytes(WAV[:-2] + b"\x01\x00")
            raise CommitUnknown(request_id)
        assert ops is calls[0][0] and context is calls[0][1] and request_id == calls[0][2]
        return storage.client("adapter").write(ops, context)

    remote.write.side_effect = write
    monkeypatch.setattr("ear_adapter.runtime.time.sleep", lambda _: None)
    outcome = ingest_native(
        capture, root=tmp_path, storage=remote, provider=_FakeProvider(), config=AdapterConfig()
    )
    if mutate:
        assert isinstance(outcome, Pending) and len(calls) == 1 and not storage.events
    else:
        assert isinstance(outcome, Ingested) and len(calls) == 2 and len(storage.events) == 5


def test_batch_limit_never_splits(tmp_path: Path, capture: CaptureInput) -> None:
    remote = Mock(spec=RemoteStorageClient)
    remote.get.return_value = None
    remote.write.side_effect = ResourceLimit()
    outcome = ingest_native(
        capture, root=tmp_path, storage=remote, provider=_FakeProvider(), config=AdapterConfig()
    )
    assert isinstance(outcome, Failed)
    assert remote.write.call_count == 1 and len(remote.write.call_args.args[0]) == 5


@pytest.mark.parametrize(
    "response", ["success", "definite_rejection", "lost_commit", "bad_receipt"]
)
def test_recording_worker_native_publication(tmp_path: Path, response: str) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = intake.accept(
        io.BytesIO(WAV),
        source="web",
        source_id=None,
        started_at=format_utc(START),
        media_type="audio/wav",
    )
    authority = MemoryStorage().client("adapter")
    sent = 0

    def duration(_job: RecordingJob, audio: BinaryIO) -> timedelta:
        assert audio.read() == WAV
        return END - START

    def endpoint(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        assert request.method == "POST" and request.url.path == "/v1/batches"
        message = json.loads(request.content)
        assert message.pop("request_id") == receipt.id
        ops, context = decode_request(json.dumps(message))
        if response == "definite_rejection":
            return httpx.Response(413)
        result = authority.write(ops, context)
        if response == "lost_commit":
            raise httpx.ReadTimeout("private response body")
        if response == "bad_receipt":
            result = replace(result, results=result.results[:-1])
        return httpx.Response(200, text=encode_result(result))

    provider = _FakeProvider()
    prepare = NativeRecordingPreparation(provider, FIXTURE_CONFIG, duration)
    with RemoteStorageClient(
        "S" * 43, "http://127.0.0.1", transport=httpx.MockTransport(endpoint)
    ) as remote:
        with intake.worker() as worker:
            assert worker.run_one(prepare, NativeRecordingPublication(remote))
            assert worker.get_result(receipt.id) is not None
        with RecordingIntake(intake.root).worker() as worker:
            assert not worker.run_one(prepare, NativeRecordingPublication(remote))
    assert provider.calls == ["asr", "speaker", "ambient"]
    assert sent == 1
    status = intake.get(receipt.id)
    assert status is not None
    if response == "definite_rejection":
        assert status["state"] == "failed"
        assert authority.count(Query(data_type="audio_ref")) == 0
    else:
        assert status["state"] == ("succeeded" if response == "success" else "blocked")
        records = authority.query(Query(data_type="audio_ref")).items
        assert len(records) == 1
        audio_ref = records[0]
        assert isinstance(audio_ref, RawRecord)
        assert audio_ref.event_start == START and audio_ref.event_end == END
        assert (intake.root / audio_ref.payload["file_ref"]["path"]).read_bytes() == WAV
        assert authority.count(Query(data_type="transcript_segment")) == 1
