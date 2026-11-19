from __future__ import annotations

import json
import signal
from collections import Counter
from unittest.mock import Mock

import pytest
from ear_adapter import Failed, Ingested
from ear_adapter.__main__ import main
from ear_adapter.runtime import Report
from ear_storage_client import PermissionDenied
from ear_storage_client.remote_errors import AuthenticationError, ServiceUnavailable


@pytest.fixture
def cli(monkeypatch):
    for name in ("EAR_URL", "EAR_TOKEN", "STORAGE_URL", "STORAGE_TOKEN"):
        monkeypatch.setenv("EAR_ADAPTER_" + name, "SECRET")
    monkeypatch.setenv("EAR_ADAPTER_EAR_URL", "http://127.0.0.1:1/internal")
    monkeypatch.setattr("ear_adapter.__main__.configure_logging", lambda: None)
    remote = Mock()
    monkeypatch.setattr("ear_adapter.__main__.RemoteStorageClient", remote)
    runner = Mock()
    monkeypatch.setattr("ear_adapter.__main__.Runner", runner)
    runner.return_value.run_once.return_value = Report()
    return remote, runner


@pytest.mark.parametrize("command", ["dry-run", "--dry-run"])
def test_dry_run_legacy(cli, capsys, command):
    remote, runner = cli
    assert main([command]) == 0
    assert runner.call_count == 2
    remote.assert_not_called()
    assert set(json.loads(capsys.readouterr().out)) == {"first", "rescan"}


@pytest.mark.parametrize(
    "report,code",
    [
        (Report(), 0),
        (Report(source_error="offline"), 1),
        (Report(storage_error="offline"), 1),
        (Report(outcomes=[Failed("x", "invalid")]), 1),
    ],
)
def test_once_status(cli, capsys, report, code):
    remote, runner = cli
    runner.return_value.run_once.return_value = report
    assert main(["once"]) == code
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert json.loads(output)["message"] == "scan"
    remote.return_value.close.assert_called_once()


@pytest.mark.parametrize("error", [PermissionDenied("SECRET"), AuthenticationError("SECRET")])
def test_configuration_error_exits_safely(cli, capsys, error):
    _, runner = cli
    runner.return_value.run_once.side_effect = error
    assert main(["once"]) == 2
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_run_recovers_and_finishes_scan(cli, monkeypatch, capsys, sig):
    remote, runner = cli
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda s, h: handlers.setdefault(s, h))
    waits = []
    monkeypatch.setattr("ear_adapter.__main__.Event.wait", lambda self, t: waits.append(t))
    calls = 0

    def scan(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Report(source_error="offline")
        if calls == 2:
            return Report(storage_error="offline")
        handlers[sig](sig, None)
        assert kwargs["should_stop"]()
        return Report(outcomes=[Ingested("finished", Counter(), 0)])

    runner.return_value.run_once.side_effect = scan
    # 启动时服务不可用也不退出。
    remote.side_effect = [ServiceUnavailable(), Mock()]
    assert main(["run", "--interval", "1"]) == 0
    assert calls == 3
    assert len(capsys.readouterr().out.splitlines()) == 4
    assert all(t == 1 for t in waits)


@pytest.mark.parametrize("interval", ["0", "-1", "86401", "nan", "inf"])
def test_invalid_interval(interval):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--interval", interval])
    assert exc.value.code == 2
