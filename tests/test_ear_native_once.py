from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from ear_storage_client import AppendRaw, MemoryStorage
from ear_storage_client.wire import decode_request, encode_read_result, encode_result

if TYPE_CHECKING:
    import ear_native_once as cli
else:
    script = Path(__file__).resolve().parents[1] / "tools/ear_native_once.py"
    spec = importlib.util.spec_from_file_location("ear_native_once", script)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)

_SCRIPT = Path(__file__).resolve().parents[1] / "tools/ear_native_once.py"
_TOKEN = "Synthetic_" + "A" * 33
_PRIVATE = "private-fixture-value"
_START = "2026-11-01T12:00:00Z"
_END = "2026-11-01T12:00:01Z"
_STEM = "segment_0_from_12-00-00_to_12-00-01"
_LOCATOR = (
    "inbox/brain/fake-device/2026-11-01/phone/12-00-00__fake-group/"
    "segment_0_richVoice_from_12-00-00_to_12-00-01.json"
)

# The CLI exercises its real wrappers and bounded subprocess transport; the
# executable only validates synthetic PCM and returns the test wire payload.
_CHILD = f"""
import json, os, sys, wave, io
from pathlib import Path
counter, mode, *args = sys.argv[1:]
assert args == ['--started-at', '{_START}', '--ended-at', '{_END}']
assert 'EAR_ADAPTER_EAR_TOKEN' not in os.environ
with Path(counter).open('a') as handle:
    handle.write('1\\n')
with wave.open(io.BytesIO(sys.stdin.buffer.read()), 'rb') as wav:
    assert wav.getparams()[:4] == (1, 2, 16000, 16000)
    assert wav.readframes(16000) == b'\\x01\\x00' * 16000
print('{_PRIVATE}', file=sys.stderr)
if mode == 'garbage':
    print('{_PRIVATE}')
    sys.exit(0)
if mode == 'partial':
    print(json.dumps({{'status': 'partial', 'detail': '{_PRIVATE}'}}))
    sys.exit(3)
span = {{'started_at': '{_START}', 'ended_at': '{_END}', 'model_version': 'fake-v1'}}
segment = dict(span, audio_chunk_id='chunk', speech_segment_id='segment',
               original_text='{_PRIVATE}', local_speaker_id='speaker',
               diarization_scope_id='chunk', language='en', overlap=False)
speaker = dict(resolution_id='resolution', diarization_scope_id='chunk',
               local_speaker_id='speaker', evidence_segment_ids=['segment'],
               embedding=None, sample_quality=None)
event = dict(span, audio_event_id='event', original_label='fake-event', taxonomy='audioset-527')
print(json.dumps(dict(status='complete',
    asr=dict(status='complete', chunks=[dict(span, audio_chunk_id='chunk')], segments=[segment]),
    speakers=[speaker], ambient=[event])))
"""


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, dict[str, Any], Path]:
    root = tmp_path / "recordings"
    manifest = root / _LOCATOR
    manifest.parent.mkdir(parents=True)
    audio = manifest.with_name(f"{_STEM}__01__fake.wav")
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 16_000)
    manifest.write_text(
        json.dumps(
            {
                "id": "fake-record",
                "group_id": "fake-group",
                "type": "richVoice",
                "timestamp": _START,
                "segment_index": 0,
                "segment_start_ms": 0,
                "segment_end_ms": 1_000,
                "files": [
                    {
                        "stored_name": audio.name,
                        "field_name": "file",
                        "size": audio.stat().st_size,
                        "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                    }
                ],
            }
        )
    )
    # An unrelated manifest must never be opened; a FIFO would block a naive reader.
    os.mkfifo(manifest.with_name("unrelated.json"), 0o600)
    worker = tmp_path / "fake_process.py"
    worker.write_text(_CHILD)
    counter = tmp_path / "process_calls"
    return (
        tmp_path / "private.json",
        {
            "record": _LOCATOR,
            "data_root": str(root),
            "launcher_argv": [sys.executable, "-I", "-B", str(worker), str(counter), "success"],
            # WAV normalization takes the no-subprocess path, no local FFmpeg needed.
            "ffmpeg_path": sys.executable,
            "ffprobe_path": sys.executable,
            "adapter_version": "ear-native/0.1.0+synthetic",
            "offline_isolated": True,
            "storage_url": "http://127.0.0.1:3217",
            "storage_token": _TOKEN,
        },
        counter,
    )


def _write(path: Path, settings: dict[str, Any] | str) -> None:
    path.write_text(settings if isinstance(settings, str) else json.dumps(settings))
    path.chmod(0o600)


def _reported(
    stdout: str | bytes, stderr: str | bytes, status: str, *, written: int = 0, remote: bool = False
) -> None:
    assert not stderr
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout) == {
        "status": status,
        "written": written,
        "deduplicated": 0,
        "mode": "remote" if remote else "dry_run",
    }


def _no_transport(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    attempted = []

    def transport(**_kwargs: Any) -> Any:
        attempted.append(True)
        pytest.fail("transport constructed")

    monkeypatch.setattr(cli.httpx, "HTTPTransport", transport)
    return attempted


def test_dry_run_default_never_uses_remote(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, settings, counter = sample
    _write(path, settings)
    attempted = _no_transport(monkeypatch)
    assert cli.main(["--config", str(path)]) == 0
    _reported(*capsys.readouterr(), "ingested", written=4)
    assert counter.read_text() == "1\n"
    assert attempted == []


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("success", "ingested", 0),
        ("garbage", "failed", 1),
        ("partial", "failed", 1),
    ],
)
def test_cli_subprocess_only_prints_closed_summary(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: str,
    code: int,
) -> None:
    path, settings, counter = sample
    settings["launcher_argv"][-1] = mode
    del settings["storage_url"], settings["storage_token"]
    _write(path, settings)
    monkeypatch.setenv("EAR_ADAPTER_EAR_TOKEN", _PRIVATE)
    result = subprocess.run(
        [sys.executable, "-B", str(_SCRIPT), "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == code
    _reported(result.stdout, result.stderr, status, written=4 if code == 0 else 0)
    assert counter.read_text() == "1\n"


@pytest.mark.parametrize(
    "change",
    [
        {"storage_token": _TOKEN + " "},
        {"storage_token": "!" * 43},
        {"storage_url": "http://localhost:3217"},
        {"storage_url": "http://10.0.0.1"},
        {"storage_url": "https://fake.invalid/?"},
        {"storage_url": "https://fake.invalid/#"},
        {"storage_url": "https://name:pass@fake.invalid"},
        {"storage_url": "https://fake.invalid:65536"},
        {"storage_url": "https://fake.invalid/api"},
        {"data_root": "relative"},
        {"launcher_argv": []},
        {"launcher_argv": ["relative"]},
        {"launcher_argv": [sys.executable, 7]},
        {"launcher_argv": [sys.executable, "--started-at=forbidden"]},
        {"launcher_argv": [sys.executable, f"--unknown={_TOKEN}"]},
        {"launcher_argv": [sys.executable, "x" * 4097]},
        {"ffmpeg_path": "ffmpeg"},
        {"ffprobe_path": "/nonexistent-test-only-executable"},
        {"adapter_version": "ear-native/0.1.0"},
        {"offline_isolated": 1},
        {"unknown": _PRIVATE},
        {"record": None},
    ],
)
def test_bad_config_precedes_transport_or_process(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
) -> None:
    path, settings, counter = sample
    _write(path, settings | change)
    attempted = _no_transport(monkeypatch)
    assert cli.main(["--config", str(path), "--remote"]) == 2
    _reported(*capsys.readouterr(), "invalid_config", remote=True)
    assert not counter.exists() and attempted == []


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "large",
        "nonfinite",
        "mode",
        "symlink",
        "ancestor",
        "hardlink",
        "fifo",
        "relative",
        "missing_target",
        "partial_target",
        "missing_record",
    ],
)
def test_private_file_validation(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    path, settings, counter = sample
    if kind == "missing_target":
        del settings["storage_url"], settings["storage_token"]
    elif kind == "partial_target":
        del settings["storage_token"]
    elif kind == "missing_record":
        del settings["record"]
    _write(path, settings)
    if kind == "duplicate":
        _write(path, json.dumps(settings)[:-1] + ', "offline_isolated":true}')
    elif kind == "large":
        _write(path, json.dumps(settings) + " " * 65536)
    elif kind == "nonfinite":
        _write(path, json.dumps(settings | {"offline_isolated": float("nan")}))
    elif kind == "mode":
        path.chmod(0o640)
    elif kind in {"symlink", "hardlink"}:
        linked = path.with_name("link.json")
        if kind == "symlink":
            linked.symlink_to(path)
        else:
            linked.hardlink_to(path)
        path = linked
    elif kind == "ancestor":
        real = path.parent / "private"
        real.mkdir(mode=0o700)
        path.rename(real / path.name)
        link = path.parent / "private-link"
        link.symlink_to(real, target_is_directory=True)
        path = link / path.name
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "relative":
        monkeypatch.chdir(path.parent)
        path = Path(path.name)
    attempted = _no_transport(monkeypatch)
    assert cli.main(["--config", str(path), "--remote"]) == 2
    _reported(*capsys.readouterr(), "invalid_config", remote=True)
    assert not counter.exists() and attempted == []


@pytest.mark.parametrize("record", ["/private/file.json", "../private/file.json", "missing.json"])
def test_explicit_record_validated_before_contact(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record: str,
) -> None:
    path, settings, counter = sample
    _write(path, settings | {"record": record})
    attempted = _no_transport(monkeypatch)
    assert cli.main(["--config", str(path), "--remote"]) == 2
    _reported(*capsys.readouterr(), "invalid_record", remote=True)
    assert not counter.exists() and attempted == []


def test_remote_explicit_storage_round_trip_and_already_ingested(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, settings, counter = sample
    _write(path, settings)
    store = MemoryStorage().client("adapter")
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {_TOKEN}"
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": "adapter"})
        body = json.loads(request.content)
        if request.url.path == "/v1/read":
            assert body["method"] == "get"
            return httpx.Response(
                200, text=encode_read_result("get", store.get(body["args"]["id"]))
            )
        assert request.url.path == "/v1/batches"
        assert body.pop("request_id")
        ops, context = decode_request(json.dumps(body))
        assert {op.record.data_type for op in ops if isinstance(op, AppendRaw)} == {
            "audio_ref",
            "local_speaker",
            "transcript_segment",
            "ambient_sound",
        }
        assert all(
            _LOCATOR not in json.dumps(op.record.payload) for op in ops if isinstance(op, AppendRaw)
        )
        return httpx.Response(200, text=encode_result(store.write(ops, context)))

    def mock_transport(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any):
        assert kwargs["trust_env"] is False
        assert kwargs["retries"] == 0
        return httpx.MockTransport(handler)

    monkeypatch.setattr(cli.httpx, "HTTPTransport", lambda **kw: mock_transport(serve, **kw))
    args = ["--config", str(path), "--remote"]
    assert cli.main(args) == 0
    _reported(*capsys.readouterr(), "ingested", written=4, remote=True)
    assert cli.main(args) == 0
    _reported(*capsys.readouterr(), "already_ingested", remote=True)
    assert counter.read_text() == "1\n"
    assert requests == [
        "/v1/identity",
        "/v1/read",
        "/v1/read",
        "/v1/batches",
        "/v1/identity",
        "/v1/read",
    ]


def test_partial_recognition_never_publishes(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, settings, counter = sample
    settings["launcher_argv"][-1] = "partial"
    _write(path, settings)
    calls: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": "adapter"})
        assert request.url.path == "/v1/read"
        return httpx.Response(200, text=encode_read_result("get", None))

    monkeypatch.setattr(cli.httpx, "HTTPTransport", lambda **_kw: httpx.MockTransport(serve))
    assert cli.main(["--config", str(path), "--remote"]) == 1
    _reported(*capsys.readouterr(), "failed", remote=True)
    assert calls == ["/v1/identity", "/v1/read"]
    assert counter.read_text() == "1\n"


@pytest.mark.parametrize(("code", "status"), [(503, "pending"), (403, "failed")])
def test_remote_errors_never_launch_process_or_print_body(
    sample: tuple[Path, dict[str, Any], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    status: str,
) -> None:
    path, settings, counter = sample
    _write(path, settings)

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": "adapter"})
        assert request.url.path == "/v1/read"
        return httpx.Response(code, text=_PRIVATE)

    monkeypatch.setattr(cli.httpx, "HTTPTransport", lambda **_kw: httpx.MockTransport(serve))
    assert cli.main(["--config", str(path), "--remote"]) == 1
    _reported(*capsys.readouterr(), status, remote=True)
    assert not counter.exists()


def test_arguments_do_not_echo_values(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([_PRIVATE]) == 2
    _reported(*capsys.readouterr(), "invalid_arguments")
