"""Prepare a private legacy Ear recognizer using an existing model configuration."""

import argparse
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

import yaml


def prepare(config_path: Path, service_dir: Path) -> None:
    config = yaml.safe_load(config_path.read_text())
    required = {"speech": "moss", "speaker": "ecapa", "environment": "panns"}
    if any(config["providers"].get(name) != model for name, model in required.items()):
        raise ValueError("source config must enable MOSS, ECAPA and PANNs")
    destination = service_dir
    if destination.exists():
        raise ValueError("target service directory already exists")
    credentials = {}
    for transport in config["model_transports"].values():
        if transport.get("mode") == "http":
            name = transport.get("token_env", "OCTOPUS_MODEL_API_TOKEN")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                raise ValueError("invalid model token filename")
            credentials[name] = (config_path.parent / "secrets" / name).read_bytes()
    config["providers"]["feedback_parser"] = "rule"
    # This legacy switch enables resumable HTTP jobs. Manual control leaves SSH disabled.
    config["remote_moss_server"] = {"control_mode": "manual", "process_manager": "systemd_user"}
    config["speaker_attribution"]["mode"] = "off"
    config["notifications"] = dict.fromkeys(config["notifications"], False)
    config["ingest"] = {"paused": False}
    config["legacy"] = {"enabled": True}
    config["mvp"]["enabled"] = False
    gateway = {
        "service_tokens": {"adapter": secrets.token_urlsafe(36)},
        "evidence_readers": ["adapter"],
        "embedding_readers": ["adapter"],
    }
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "service"
        staging.mkdir(mode=0o700)
        (staging / "secrets").mkdir(mode=0o700)
        (staging / "config.yaml").write_text(yaml.safe_dump(config))
        (staging / "secrets/gateway.json").write_text(json.dumps(gateway))
        for name, value in credentials.items():
            (staging / "secrets" / name).write_bytes(value)
        staging.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--service-dir", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.config.resolve(), args.service_dir.resolve())
