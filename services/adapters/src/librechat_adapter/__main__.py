"""LibreChat 源适配器命令行：dry-run（内存）、once、run（常驻）。

    librechat-adapter dry-run [--from-file export.ndjson] [--full]
    librechat-adapter once    [--from-file export.ndjson] [--full] [--config private.json]
    librechat-adapter run     [--interval 300] [--config private.json]
    librechat-adapter registry            # 打印三类数据的登记定义，供 ① 负责人登记

配置只来自环境变量或 --config 指向的私有 JSON（键同环境变量；文件必须是 0600、属于当前用户、
不是符号链接）。令牌不进命令行和日志。退出码：0 完整成功；1 暂时失败或有被拒绝的条目，下轮重试；
2 配置、权限、协议或文件卷问题；3 LibreChat 版本/导出格式不在已核对范围内。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from threading import Event

from ear_contracts import UnknownDataTypeError
from ear_storage_client import InvalidRequest, MemoryStorage, PermissionDenied, StorageClient
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, ServiceUnavailable
from ear_storage_client.remote_errors import ProtocolError as StorageProtocolError

from librechat_adapter.config import LibreChatConfig
from librechat_adapter.engine import Ingestor
from librechat_adapter.registry import chat_specs, with_chat_types
from librechat_adapter.runner import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RETRY,
    Runner,
    RunResult,
    preflight,
)
from librechat_adapter.sources import CommandSource, ExportSource, FileSource
from librechat_adapter.state import RunState
from librechat_adapter.volume import PrivateVolume, VolumeError

PREFIX = "EAR_LIBRECHAT_"


class ConfigurationError(Exception):
    """配置缺失或无效。消息固定，不含值。"""


@dataclass(frozen=True, slots=True)
class Settings:
    config: LibreChatConfig
    file_root: Path | None
    file_mode: int
    storage_url: str | None
    storage_token: str | None
    export_argv: tuple[str, ...] | None
    export_timeout: float
    accept_versions: frozenset[str]
    full_every: timedelta | None


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


def read_private_file(path: Path) -> str:
    """读取只属于当前用户的私有文件（配置或令牌）。"""
    try:
        st = os.lstat(path)
    except OSError:
        raise ConfigurationError("无法读取私有配置文件") from None
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or st.st_mode & 0o077
        or st.st_size > 64 * 1024
    ):
        raise ConfigurationError("私有文件必须是属于当前用户、权限 0600 的普通小文件")
    return path.read_text(encoding="utf-8")


def merged_environment(config_file: Path | None, base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    if config_file is not None:
        try:
            values = json.loads(read_private_file(config_file.expanduser()))
        except ValueError:
            raise ConfigurationError("私有配置不是 JSON") from None
        if not isinstance(values, dict) or any(
            not isinstance(k, str) or not k.startswith(PREFIX) or not isinstance(v, str)
            for k, v in values.items()
        ):
            raise ConfigurationError("私有配置必须是 EAR_LIBRECHAT_* 字符串映射")
        env.update(values)
    return env


def load_settings(env: Mapping[str, str]) -> Settings:
    def get(name: str) -> str | None:
        return env.get(PREFIX + name) or None

    token = get("STORAGE_TOKEN")
    if (token_file := get("STORAGE_TOKEN_FILE")) is not None:
        token = read_private_file(Path(token_file).expanduser()).strip()
    argv: tuple[str, ...] | None = None
    if (raw := get("EXPORT_ARGV")) is not None:
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ConfigurationError("EXPORT_ARGV 必须是 JSON 字符串数组") from None
        if (
            not isinstance(parsed, list)
            or not parsed
            or not all(isinstance(a, str) and a for a in parsed)
        ):
            raise ConfigurationError("EXPORT_ARGV 必须是 JSON 字符串数组")
        argv = tuple(parsed)
    try:
        mode = int(get("FILE_MODE") or "640", 8)
        full_hours = float(get("FULL_EVERY_HOURS") or "168")
        timeout = float(get("EXPORT_TIMEOUT") or "3600")
        accept = frozenset(
            v.strip() for v in (get("ACCEPT_VERSIONS") or "").split(",") if v.strip()
        )
        config = LibreChatConfig(device=get("DEVICE") or "librechat")
    except ValueError:
        raise ConfigurationError("环境变量值无效") from None
    if not math.isfinite(full_hours) or full_hours < 0 or not 1 <= timeout <= 86400:
        raise ConfigurationError("FULL_EVERY_HOURS 或 EXPORT_TIMEOUT 超出范围")
    root = get("FILE_ROOT")
    return Settings(
        config=config,
        file_root=Path(root).expanduser() if root else None,
        file_mode=mode,
        storage_url=get("STORAGE_URL"),
        storage_token=token,
        export_argv=argv,
        export_timeout=timeout,
        accept_versions=accept,
        full_every=timedelta(hours=full_hours) if full_hours > 0 else None,
    )


def _source(settings: Settings, from_file: Path | None) -> ExportSource:
    limit = settings.config.max_line_bytes
    if from_file is not None:
        return FileSource(from_file, max_line_bytes=limit)
    if settings.export_argv is None:
        raise ConfigurationError("需要 EAR_LIBRECHAT_EXPORT_ARGV 或 --from-file")
    return CommandSource(
        settings.export_argv, max_line_bytes=limit, timeout=settings.export_timeout
    )


def _interval(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 1 <= seconds <= 86400:
        raise argparse.ArgumentTypeError("interval 必须在 1–86400 秒之间")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="librechat-adapter", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("registry")
    for name in ("dry-run", "once", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--from-file", type=Path, default=None)
        sub.add_argument("--config", type=Path, default=None)
        sub.add_argument("--accept-version", action="append", default=[])
        if name != "run":
            sub.add_argument("--full", action="store_true")
        if name == "run":
            sub.add_argument("--interval", type=_interval, default=300.0)
    return parser


def _dry_run(settings: Settings, from_file: Path | None, full: bool) -> int:
    memory = MemoryStorage(registry=with_chat_types())
    storage = memory.client("adapter")
    with tempfile.TemporaryDirectory(prefix="ear-librechat-dry-") as tmp:
        root = Path(tmp)
        root.chmod(0o700)
        volume = PrivateVolume(root, file_mode=settings.file_mode)
        results: dict[str, RunResult] = {}
        for label in ("first", "rescan"):
            ingestor = Ingestor(
                storage, volume, settings.config, accept_versions=settings.accept_versions
            )
            runner = Runner(
                source=_source(settings, from_file), ingestor=ingestor, state=None, full_every=None
            )
            results[label] = runner.run_once(force_full=True)
        print(
            json.dumps({k: v.summary() for k, v in results.items()}, ensure_ascii=False, indent=2)
        )
        return max(r.exit_code for r in results.values())


def _remote(settings: Settings) -> RemoteStorageClient:
    if not settings.storage_url or not settings.storage_token:
        raise ConfigurationError("需要 EAR_LIBRECHAT_STORAGE_URL 与 STORAGE_TOKEN(_FILE)")
    return RemoteStorageClient(settings.storage_token, settings.storage_url, module="adapter")


def _one_round(
    settings: Settings, from_file: Path | None, force_full: bool, volume: PrivateVolume
) -> RunResult:
    source = _source(settings, from_file)  # 先做不联网的配置检查
    storage: StorageClient = _remote(settings)
    try:
        preflight(storage)
        runner = Runner(
            source=source,
            ingestor=Ingestor(
                storage, volume, settings.config, accept_versions=settings.accept_versions
            ),
            state=RunState(volume.root, settings.config.device, dir_mode=volume.dir_mode),
            full_every=settings.full_every,
        )
        return runner.run_once(force_full=force_full)
    finally:
        close = getattr(storage, "close", None)
        if close is not None:
            close()


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "registry":
        print(json.dumps({s.name: s.to_json() for s in chat_specs()}, ensure_ascii=False, indent=2))
        return EXIT_OK
    try:
        merged = merged_environment(args.config, os.environ if env is None else env)
        settings = load_settings(merged)
        if args.accept_version:
            settings = replace(
                settings,
                accept_versions=settings.accept_versions | frozenset(args.accept_version),
            )
        if args.command == "dry-run":
            return _dry_run(settings, args.from_file, args.full)
        if settings.file_root is None:
            raise ConfigurationError("需要 EAR_LIBRECHAT_FILE_ROOT")
        volume = PrivateVolume(settings.file_root, file_mode=settings.file_mode)
    except (ConfigurationError, VolumeError) as exc:
        _emit("configuration_error", reason=str(exc))
        return EXIT_CONFIG

    stop = Event()
    previous = {}
    if args.command == "run":
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        while True:
            try:
                result = _one_round(
                    settings, args.from_file, bool(getattr(args, "full", False)), volume
                )
            except ServiceUnavailable:
                _emit("scan", status="storage_unavailable")
                code = EXIT_RETRY
            except ConfigurationError as exc:
                _emit("configuration_error", reason=str(exc))
                return EXIT_CONFIG
            except VolumeError:
                _emit("configuration_error", reason="私有文件卷不可用或其中的目录被改动")
                return EXIT_CONFIG
            except (
                AuthenticationError,
                PermissionDenied,
                InvalidRequest,
                StorageProtocolError,
                UnknownDataTypeError,
            ) as exc:
                _emit(
                    "configuration_error",
                    error=type(exc).__name__,
                    reason="检查 ① 身份、chat_session/chat_message/chat_attachment 的读权限与登记",
                )
                return EXIT_CONFIG
            else:
                _emit("scan", **result.summary())
                code = result.exit_code
                if code in (EXIT_CONFIG, 3):
                    return code
            if args.command != "run":
                return code
            if stop.wait(args.interval):
                return EXIT_OK
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
