"""Temporarily bind the macOS ear-ingest LaunchAgent to loopback for relay tests."""

import argparse
import fcntl
import os
import plistlib
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

LABEL = "com.prts.ear-ingest"


def bootstrap(domain: str, config: Path) -> None:
    # bootout can return before the previous registration has been removed.
    last_error = ""
    for _ in range(40):
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, config], capture_output=True, text=True
        )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip()
        time.sleep(0.5)
    raise RuntimeError(last_error)


def listeners() -> set[str]:
    result = subprocess.run(
        ["lsof", "-nP", "-iTCP:8791", "-sTCP:LISTEN", "-Fn"], capture_output=True, text=True
    )
    return {line[1:] for line in result.stdout.splitlines() if line.startswith("n")}


def verify_listener(address: str) -> None:
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(40):
        if listeners() == {address}:
            try:
                with http.open("http://127.0.0.1:8791/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
        time.sleep(0.25)
    raise RuntimeError(f"Could not verify healthy listener at {address}")


def isolate(seconds: int) -> None:
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    source = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    config = plistlib.loads(source.read_bytes())
    arguments = config["ProgramArguments"]
    old = "--host 0.0.0.0 --port 8791"
    if config.get("Label") != LABEL or sum(arg.count(old) for arg in arguments) != 1:
        raise ValueError("Expected the documented ear-ingest LaunchAgent command")
    config["ProgramArguments"] = [a.replace(old, "--host 127.0.0.1 --port 8791") for a in arguments]
    support = Path.home() / "Library/Application Support/EarIngest"
    support.mkdir(exist_ok=True, parents=True)
    with (support / "isolation.lock").open("a") as lock, tempfile.TemporaryDirectory() as directory:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_listener("*:8791")
        temporary = Path(directory) / "loopback.plist"
        temporary.write_bytes(plistlib.dumps(config))
        subprocess.run(["launchctl", "bootout", service], check=True)
        try:
            bootstrap(domain, temporary)
            verify_listener("127.0.0.1:8791")
            print("Direct uploads isolated. USB debugging stays available.", flush=True)
            time.sleep(seconds)
        finally:
            subprocess.run(["launchctl", "bootout", service], capture_output=True)
            bootstrap(domain, source)
            verify_listener("*:8791")
            print("Normal ear-ingest listener restored.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", required=True, type=int)
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error("--seconds must be positive")

    def stop(signum, frame):
        raise SystemExit()

    signal.signal(signal.SIGTERM, stop)
    isolate(args.seconds)
