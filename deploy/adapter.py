"""Launch the archive adapter using a private JSON environment map or environment."""

import argparse
import json
import os
from pathlib import Path

from ear_adapter.__main__ import main as adapter_main


def configure(path: Path | None) -> None:
    if path is not None:
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or any(
            not key.startswith("EAR_ADAPTER_") or not isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError("Expected an EAR_ADAPTER environment map")
        os.environ.update(values)
    if gateway := os.getenv("EAR_ADAPTER_GATEWAY_FILE"):
        os.environ["EAR_ADAPTER_EAR_TOKEN"] = json.loads(Path(gateway).expanduser().read_text())[
            "service_tokens"
        ]["adapter"]
    if token := os.getenv("EAR_ADAPTER_STORAGE_TOKEN_FILE"):
        os.environ["EAR_ADAPTER_STORAGE_TOKEN"] = Path(token).expanduser().read_text().strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args, remaining = parser.parse_known_args()
    try:
        configure(args.config)
    except (ValueError, OSError, KeyError):
        parser.exit(2, "Invalid private adapter configuration\n")
    return adapter_main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
