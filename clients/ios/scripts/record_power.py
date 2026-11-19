"""Capture Power Profiler data attributed to the running EarRecorder iOS process."""

import argparse
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

IMPACTS = ("cpu-impact", "display-impact", "gpu-impact", "networking-impact")


def run(*args):
    stdout = subprocess.DEVNULL if args[0] == "devicectl" else None
    subprocess.run(["xcrun", *map(str, args)], stdout=stdout, check=True)


def capture(directory, device, seconds):
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "devices.json"
        run("devicectl", "list", "devices", "--json-output", output)
        devices = [
            item
            for item in json.loads(output.read_text())["result"]["devices"]
            if item["connectionProperties"].get("tunnelState") == "connected"
            and item["hardwareProperties"].get("platform") == "iOS"
            and device in (None, item["identifier"], item["hardwareProperties"]["udid"])
        ]
        if len(devices) != 1:
            raise SystemExit("Connect one iPhone or select its identifier with --device.")
        phone = devices[0]
        run(
            "devicectl",
            "device",
            "info",
            "processes",
            "--device",
            phone["identifier"],
            "--json-output",
            output,
        )
        pids = [
            p["processIdentifier"]
            for p in json.loads(output.read_text())["result"]["runningProcesses"]
            if p.get("executable", "").endswith("/EarRecorder.app/EarRecorder")
        ]
        if len(pids) != 1:
            raise SystemExit("Start recording in EarRecorder before collecting a trace.")
        trace = directory / "recording.trace"
        run(
            "xctrace",
            "record",
            "--template",
            "Power Profiler",
            "--device",
            phone["hardwareProperties"]["udid"],
            "--attach",
            pids[0],
            "--time-limit",
            f"{seconds}s",
            "--output",
            trace,
        )
        return trace


def summarize(trace, directory):
    with tempfile.TemporaryDirectory() as temp:
        toc = Path(temp) / "toc.xml"
        table = Path(temp) / "table.xml"
        run("xctrace", "export", "--input", trace, "--toc", "--output", toc)
        info = ET.parse(toc).find("./run[@number='1']/info")
        if info is None:
            raise ValueError("Trace has no first run")
        run(
            "xctrace",
            "export",
            "--input",
            trace,
            "--xpath",
            '/trace-toc/run[@number="1"]/data/table[@schema="ProcessSubsystemPowerImpact"]',
            "--output",
            table,
        )
        root = ET.parse(table)
    cols = [n.text for n in root.findall(".//schema/col/mnemonic")]
    if not {"process-name", "process", "duration", "start", *IMPACTS} <= set(cols):
        raise ValueError("Process power columns unavailable")
    refs = {n.attrib["id"]: n for n in root.iter() if "id" in n.attrib}

    def resolve(node):
        return refs[node.attrib["ref"]] if "ref" in node.attrib else node

    stats = {k: [0.0, 0.0] for k in IMPACTS}
    samples = []
    seen = set()
    for row in root.findall(".//row"):
        cells = dict(zip(cols, map(resolve, row), strict=True))
        if cells["process-name"].text != "EarRecorder":
            continue
        pid = cells["process"].find("pid")
        if pid is None:
            continue
        duration = int(cells["duration"].text) / 1e9
        sample = {
            "pid": int(resolve(pid).text),
            "start_s": int(cells["start"].text) / 1e9,
            "duration_s": duration,
        }
        for key in IMPACTS:
            value = cells[key].text
            if value is not None:
                sample[key] = float(value)
        # xctrace can repeat the final interval when the run ends.
        signature = tuple(sample.items())
        if signature in seen:
            continue
        seen.add(signature)
        for key in IMPACTS:
            if key in sample:
                stats[key][0] += duration * sample[key]
                stats[key][1] += duration
        samples.append(sample)
    if not samples or not any(v[1] for v in stats.values()):
        raise ValueError("No attributed EarRecorder power samples in run 1")
    with (directory / "samples.jsonl").open("w") as log:
        for sample in samples:
            print(json.dumps(sample), file=log)
    summary = {
        "process": "EarRecorder",
        "start": info.findtext("summary/start-date"),
        "end": info.findtext("summary/end-date"),
        "unit": "Instruments power impact score",
        "subsystems": {
            k: {"mean": total / span if span else None, "sampled_seconds": span}
            for k, (total, span) in stats.items()
        },
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New private output directory")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--device", help="UDID or CoreDevice identifier")
    parser.add_argument(
        "--trace", type=Path, help="Analyze run 1 of an existing Power Profiler trace"
    )
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    os.umask(0o077)
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    trace = args.trace or capture(directory, args.device, args.seconds)
    summarize(trace, directory)


if __name__ == "__main__":
    main()
