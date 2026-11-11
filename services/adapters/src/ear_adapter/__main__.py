"""② 命令行：内存试运行、远程单轮导入、常驻扫描。凭据只读取环境变量。"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import urllib.error
from pathlib import Path
from threading import Event

from ear_storage_client import InvalidRequest, MemoryStorage, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, ProtocolError, ServiceUnavailable

from ear_adapter.config import AdapterConfig
from ear_adapter.runtime import Failed, Runner, configure_logging
from ear_adapter.source import HttpEarSource, InboxAudioLocator


def _interval(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 1 <= seconds <= 86400:
        raise argparse.ArgumentTypeError("interval 必须在 1–86400 秒之间")
    return seconds


def _emit(message: str, **fields: object) -> None:
    print(
        json.dumps(
            dict(
                module="adapter",
                event_id=None,
                root_feedback_id="system",
                depth=0,
                message=message,
                **fields,
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ear-adapter")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--dry-run"]:
        argv = ["dry-run"]  # legacy alias；新脚本使用 dry-run。
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("once")
    commands.add_parser("run").add_argument("--interval", type=_interval, default=60.0)
    args = parser.parse_args(argv)
    required = ["EAR_ADAPTER_EAR_URL", "EAR_ADAPTER_EAR_TOKEN"]
    if args.command != "dry-run":
        required += ["EAR_ADAPTER_STORAGE_URL", "EAR_ADAPTER_STORAGE_TOKEN"]
    if any(not os.environ.get(name) for name in required):
        _emit("configuration_error", reason="需要环境变量 " + ", ".join(required))
        return 2
    data_root = Path(os.environ.get("EAR_ADAPTER_DATA_ROOT", "~/.octopus/local")).expanduser()
    config = AdapterConfig(data_root=data_root)
    configure_logging()
    stop = Event()
    previous = {}
    remote: RemoteStorageClient | None = None
    if args.command == "run":
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                if args.command == "dry-run":
                    storage = MemoryStorage().client("adapter")
                else:
                    remote = RemoteStorageClient(
                        os.environ["EAR_ADAPTER_STORAGE_TOKEN"],
                        os.environ["EAR_ADAPTER_STORAGE_URL"],
                        module="adapter",
                    )
                    storage = remote
                runner = Runner(
                    source=HttpEarSource(
                        os.environ["EAR_ADAPTER_EAR_URL"], os.environ["EAR_ADAPTER_EAR_TOKEN"]
                    ),
                    storage=storage,
                    locator=InboxAudioLocator(data_root),
                    config=config,
                )
                while not stop.is_set():
                    report = runner.run_once(should_stop=stop.is_set)
                    if args.command == "dry-run":
                        rescan = Runner(
                            source=runner.source,
                            storage=storage,
                            locator=runner.locator,
                            config=config,
                        ).run_once()
                        print(
                            json.dumps(
                                {"first": report.summary(), "rescan": rescan.summary()},
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                        return 0
                    _emit("scan", **report.summary())
                    if args.command == "once":
                        return int(
                            bool(
                                report.source_error
                                or report.storage_error
                                or any(isinstance(o, Failed) for o in report.outcomes)
                            )
                        )
                    stop.wait(args.interval)
            except ServiceUnavailable:
                # SDK 构造时也会请求 identity/registry；启动期间掉线同样重试。
                _emit("scan", storage_error="① 暂时不可用")
                if args.command != "run":
                    return 1
                stop.wait(args.interval)
            finally:
                if remote is not None:
                    remote.close()
                    remote = None
    except (
        AuthenticationError,
        PermissionDenied,
        InvalidRequest,
        ProtocolError,
        urllib.error.HTTPError,
    ) as exc:
        # 不记录异常正文、URL 或 HTTP body，避免配置凭据被带入日志。
        _emit(
            "configuration_error",
            error=type(exc).__name__,
            reason="检查身份、adapter 七种原始类型 read_types、旧 Ear 授权及服务配置",
        )
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
