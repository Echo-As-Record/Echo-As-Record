"""CLI configuration, redaction and graceful stop with fake services."""

import signal
from unittest.mock import MagicMock, Mock

import pytest
from geo_adapter.__main__ import main
from geo_adapter.providers import ConfigurationError


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("GEO_ADAPTER_STORAGE_URL", "https://storage.invalid")
    monkeypatch.setenv("GEO_ADAPTER_STORAGE_TOKEN", "SECRET")
    monkeypatch.setenv("GEO_ADAPTER_NOMINATIM_URL", "https://map.invalid")
    monkeypatch.setenv("GEO_ADAPTER_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GEO_ADAPTER_AMAP_KEY", raising=False)
    monkeypatch.delenv("GEO_ADAPTER_MAX_WAIT_SECONDS", raising=False)
    remote, runner = MagicMock(), Mock()
    monkeypatch.setattr("geo_adapter.__main__.RemoteStorageClient", remote)
    monkeypatch.setattr("geo_adapter.__main__.Runner", runner)
    runner.return_value.run_once.return_value = {"written": 0}
    return remote, runner


def test_dry_run_count_only(cli):
    _, runner = cli
    assert main(["dry-run"]) == 0
    assert runner.return_value.run_once.call_args.kwargs["dry_run"] is True


@pytest.mark.parametrize("failed,code", [(0, 0), (1, 1)])
def test_once(cli, failed, code):
    _, runner = cli
    runner.return_value.run_once.return_value = {"failed": failed}
    assert main(["once"]) == code


def test_secret_error_redacted(cli, capsys):
    _, runner = cli
    runner.return_value.run_once.side_effect = ConfigurationError("SECRET 35.0 synthetic-place")
    assert main(["once"]) == 2
    output = capsys.readouterr().out
    assert "SECRET" not in output and "35.0" not in output and "synthetic-place" not in output


@pytest.mark.parametrize(
    "name,value",
    [
        ("GEO_ADAPTER_DELAY", "nan"),
        ("GEO_ADAPTER_MAX_WAIT_SECONDS", "nan"),
        ("GEO_ADAPTER_MAX_WAIT_SECONDS", "inf"),
        ("GEO_ADAPTER_MAX_WAIT_SECONDS", "-1"),
        ("GEO_ADAPTER_ATTEMPTS", "2.5"),
        ("GEO_ADAPTER_TIMEOUT", "0"),
        ("GEO_ADAPTER_TIMEOUT", "61"),
    ],
)
def test_configuration_bounds(cli, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert main(["once"]) == 2
    cli[0].assert_not_called()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("command", ["once", "run"])
def test_signal_finishes_current_work(cli, monkeypatch, sig, command):
    _, runner = cli
    handlers = {}

    def register(s, handler):
        handlers[s] = handler
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", register)

    def scan(**kwargs):
        assert not kwargs["should_stop"]()
        handlers[sig](sig, None)
        assert kwargs["should_stop"]()
        return {"written": 1}

    runner.return_value.run_once.side_effect = scan
    assert main([command]) == 0
    assert runner.return_value.run_once.call_count == 1


@pytest.mark.parametrize("value,expected", [(None, 21600), ("120", 120), ("0", 0)])
def test_max_wait_configuration(cli, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("GEO_ADAPTER_MAX_WAIT_SECONDS", value)
    assert main(["once"]) == 0
    assert cli[1].call_args.kwargs["max_wait"] == expected
