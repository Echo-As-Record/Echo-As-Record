"""命令行、配置解析、登记表与帧解析。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec
from ear_storage_client import InvalidRequest, PermissionDenied, Query
from ear_storage_client.remote_errors import ServiceUnavailable
from librechat_adapter import __main__ as cli
from librechat_adapter.registry import CHAT_TYPES, chat_specs, with_chat_types
from librechat_adapter.testing import (
    attachment,
    blob,
    conversation,
    end,
    header,
    join,
    memory_storage,
    message,
)
from librechat_adapter.wire import ProtocolError, RejectedFrame, parse_frame

SECRET_TEXT = "这是一段不应该出现在日志里的私密内容"


def _export_file(tmp_path: Path, *, version: str = "0.8.9") -> Path:
    path = tmp_path / "export.ndjson"
    path.write_text(
        "".join(
            join(
                [
                    header(version=version),
                    conversation(),
                    blob(1, b"data" * 100),
                    attachment("a", blob_id=1),
                    message(text=SECRET_TEXT),
                    end(),
                ]
            )
        ),
        encoding="utf-8",
    )
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "vol"
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return root


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"EAR_LIBRECHAT_FILE_ROOT": str(_root(tmp_path)), **extra}


# -- registry --------------------------------------------------------------------------------------
def test_registry_command_prints_the_three_adapter_owned_raw_types(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["registry"], {}) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == set(CHAT_TYPES)
    for spec in printed.values():
        assert spec["kind"] == "raw_record" and spec["writer"] == "adapter"


def test_with_chat_types_is_idempotent_and_rejects_conflicting_definitions() -> None:
    registry = with_chat_types()
    assert with_chat_types(registry) is registry
    for name in CHAT_TYPES:
        assert registry.writer_of(name) == "adapter"
        assert registry.is_core(name)
    # 共享登记表合入后必须与本适配器的定义一致
    for spec in chat_specs():
        if spec.name in DEFAULT_REGISTRY:
            assert DEFAULT_REGISTRY.get(spec.name).writer == "adapter"
            assert DEFAULT_REGISTRY.get(spec.name).kind == "raw_record"
    wrong = DEFAULT_REGISTRY.extend_core(DataTypeSpec("chat_message", "raw_record", "api"))
    with pytest.raises(ValueError, match="chat_message"):
        with_chat_types(wrong)


# -- configuration ---------------------------------------------------------------------------------
def test_load_settings_defaults_and_validation() -> None:
    settings = cli.load_settings({})
    assert settings.config.device == "librechat"
    assert settings.full_every is not None and settings.full_every.days == 7
    assert settings.file_mode == 0o640 and settings.export_argv is None

    parsed = cli.load_settings(
        {
            "EAR_LIBRECHAT_EXPORT_ARGV": json.dumps(["ssh", "fixture-export-host", "exporter"]),
            "EAR_LIBRECHAT_FULL_EVERY_HOURS": "0",
            "EAR_LIBRECHAT_DEVICE": "fixture-export-host",
            "EAR_LIBRECHAT_ACCEPT_VERSIONS": "0.8.8, 0.9.0",
            "EAR_LIBRECHAT_FILE_MODE": "600",
        }
    )
    assert parsed.export_argv == ("ssh", "fixture-export-host", "exporter")
    assert parsed.full_every is None and parsed.file_mode == 0o600
    assert parsed.accept_versions == {"0.8.8", "0.9.0"}

    bad: list[dict[str, str]] = [
        {"EAR_LIBRECHAT_EXPORT_ARGV": "not json"},
        {"EAR_LIBRECHAT_EXPORT_ARGV": "[]"},
        {"EAR_LIBRECHAT_EXPORT_ARGV": '["ssh", 5]'},
        {"EAR_LIBRECHAT_EXPORT_ARGV": '"ssh host"'},
        {"EAR_LIBRECHAT_DEVICE": "bad device!"},
        {"EAR_LIBRECHAT_FULL_EVERY_HOURS": "nan"},
        {"EAR_LIBRECHAT_FULL_EVERY_HOURS": "-1"},
        {"EAR_LIBRECHAT_EXPORT_TIMEOUT": "0"},
        {"EAR_LIBRECHAT_FILE_MODE": "9"},
    ]
    for env in bad:
        with pytest.raises(cli.ConfigurationError):
            cli.load_settings(env)


def test_private_files_must_be_owned_regular_and_unreadable_by_others(tmp_path: Path) -> None:
    good = tmp_path / "private.json"
    good.write_text(json.dumps({"EAR_LIBRECHAT_STORAGE_TOKEN": "t" * 43}))
    good.chmod(0o600)
    env = cli.merged_environment(good, {"A": "1"})
    assert env["EAR_LIBRECHAT_STORAGE_TOKEN"] == "t" * 43 and env["A"] == "1"

    loose = tmp_path / "loose.json"
    loose.write_text("{}")
    loose.chmod(0o644)
    link = tmp_path / "link.json"
    link.symlink_to(good)
    foreign_key = tmp_path / "foreign.json"
    foreign_key.write_text(json.dumps({"OTHER_SETTING": "x"}))
    foreign_key.chmod(0o600)
    not_json = tmp_path / "text.json"
    not_json.write_text("nope")
    not_json.chmod(0o600)
    for path in (loose, link, foreign_key, not_json, tmp_path / "missing.json"):
        with pytest.raises(cli.ConfigurationError):
            cli.merged_environment(path, {})

    token = tmp_path / "token"
    token.write_text("s" * 43 + "\n")
    token.chmod(0o600)
    settings = cli.load_settings({"EAR_LIBRECHAT_STORAGE_TOKEN_FILE": str(token)})
    assert settings.storage_token == "s" * 43


# -- commands --------------------------------------------------------------------------------------
def test_dry_run_proves_replay_is_idempotent_without_touching_the_real_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = _export_file(tmp_path)
    assert cli.main(["dry-run", "--from-file", str(export)], {}) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["first"]["report"]["written"] == {
        "chat_session": 1,
        "chat_attachment": 1,
        "chat_message": 1,
    }
    assert result["rescan"]["report"]["written"] == {}
    assert result["rescan"]["report"]["unchanged"]["chat_message"] == 1
    assert result["first"]["report"]["deletions"] == "deferred"
    assert SECRET_TEXT not in out  # 摘要只有计数


def test_once_writes_through_the_client_and_never_logs_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage, client = memory_storage()
    seen: list[Any] = []
    monkeypatch.setattr(cli, "_remote", lambda settings: client)
    export = _export_file(tmp_path)
    env = _env(
        tmp_path,
        EAR_LIBRECHAT_STORAGE_URL="http://127.0.0.1:1",
        EAR_LIBRECHAT_STORAGE_TOKEN="x" * 43,
    )
    assert cli.main(["once", "--from-file", str(export)], env) == 0
    out = capsys.readouterr().out
    scan = json.loads(out.strip().splitlines()[-1])
    assert scan["message"] == "scan" and scan["status"] == "complete"
    assert scan["root_feedback_id"] == "system" and scan["module"] == "adapter"
    assert SECRET_TEXT not in out and "x" * 43 not in out
    assert len(storage.events) == 3
    items = client.query(Query("chat_message", limit=10)).items
    assert items[0].payload["text"] == SECRET_TEXT
    assert (tmp_path / "vol" / "librechat" / "state" / "librechat.json").exists()
    seen.append(cli.main(["once", "--from-file", str(export)], env))
    assert seen == [0] and len(storage.events) == 3  # 重放不新增


def test_configuration_problems_exit_2_with_fixed_messages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = _export_file(tmp_path)
    assert cli.main(["once", "--from-file", str(export)], {}) == 2  # 缺 FILE_ROOT
    assert "FILE_ROOT" in capsys.readouterr().out
    assert (
        cli.main(
            ["once"],
            _env(tmp_path, EAR_LIBRECHAT_STORAGE_URL="http://x", EAR_LIBRECHAT_STORAGE_TOKEN="t"),
        )
        == 2
    )
    assert cli.main(["once", "--from-file", str(export)], _env(tmp_path)) == 2  # 缺存储地址
    capsys.readouterr()
    bad_root = {"EAR_LIBRECHAT_FILE_ROOT": str(tmp_path / "absent")}
    assert cli.main(["once", "--from-file", str(export)], bad_root) == 2


def test_missing_registration_or_read_permission_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Denied:
        def query(self, query: Query) -> Any:
            raise PermissionDenied("no")

        def close(self) -> None:
            pass

    class Unregistered(Denied):
        def query(self, query: Query) -> Any:
            raise InvalidRequest("unknown type")

    export = _export_file(tmp_path)
    env = _env(tmp_path, EAR_LIBRECHAT_STORAGE_URL="http://x", EAR_LIBRECHAT_STORAGE_TOKEN="t" * 43)
    for stub in (Denied(), Unregistered()):
        monkeypatch.setattr(cli, "_remote", lambda settings, s=stub: s)
        assert cli.main(["once", "--from-file", str(export)], env) == 2
        assert "chat_message" in capsys.readouterr().out


def test_storage_outage_is_retryable_and_version_drift_is_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, EAR_LIBRECHAT_STORAGE_URL="http://x", EAR_LIBRECHAT_STORAGE_TOKEN="t" * 43)

    def down(settings: Any) -> Any:
        raise ServiceUnavailable()

    monkeypatch.setattr(cli, "_remote", down)
    export = _export_file(tmp_path)
    assert cli.main(["once", "--from-file", str(export)], env) == 1

    _, client = memory_storage()
    monkeypatch.setattr(cli, "_remote", lambda settings: client)
    drifted = _export_file(tmp_path, version="0.9.1")
    assert cli.main(["once", "--from-file", str(drifted)], env) == 3
    assert cli.main(["once", "--from-file", str(drifted), "--accept-version", "0.9.1"], env) == 0


# -- frames ----------------------------------------------------------------------------------------
def test_frames_ignore_unknown_fields_and_validate_identifiers() -> None:
    ok = parse_frame(message(extra_future_field=1))
    assert not isinstance(ok, RejectedFrame) and ok.t == "message"
    for bad in (
        message("a b"),
        message("a/b"),
        message(".."),
        message("x" * 201),
        message("m", "c:1"),
        message(role="system"),
        message(attachment_file_ids=["ok", "no good"]),
    ):
        assert isinstance(parse_frame(bad), RejectedFrame)
    rejected = parse_frame(message("a b"))
    assert isinstance(rejected, RejectedFrame) and "a b" not in rejected.reason
    with pytest.raises(ProtocolError):
        parse_frame('{"t":"blob","blob_id":0,"seq":0,"b64":""}')
