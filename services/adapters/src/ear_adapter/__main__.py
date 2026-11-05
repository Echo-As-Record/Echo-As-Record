"""命令行入口。

① 的持久化服务还没有实现，所以目前只提供试运行：读真实的旧 Ear，转换后写进内存存储，
打印每类数据的条数和未能导入的录音。用来在真实数据上检查解析和「不重不漏」，不落库。

    EAR_ADAPTER_EAR_URL=http://127.0.0.1:8765/internal \
    EAR_ADAPTER_EAR_TOKEN=... uv run ear-adapter --dry-run

token 只从环境变量读，不要写在命令参数里。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ear_storage_client import MemoryStorage

from ear_adapter.config import AdapterConfig
from ear_adapter.runtime import Runner, configure_logging
from ear_adapter.source import HttpEarSource, InboxAudioLocator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ear-adapter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="写进内存存储，不落库（① 的持久化服务实现之前只支持这一种）",
    )
    parser.parse_args(argv)
    url = os.environ.get("EAR_ADAPTER_EAR_URL")
    token = os.environ.get("EAR_ADAPTER_EAR_TOKEN")
    if not url or not token:
        print("需要环境变量 EAR_ADAPTER_EAR_URL 和 EAR_ADAPTER_EAR_TOKEN", file=sys.stderr)
        return 2
    data_root = Path(os.environ.get("EAR_ADAPTER_DATA_ROOT", "~/.octopus/local")).expanduser()
    config = AdapterConfig(data_root=data_root)
    configure_logging()
    storage = MemoryStorage()
    runner = Runner(
        source=HttpEarSource(url, token),
        storage=storage.client("adapter"),
        locator=InboxAudioLocator(data_root),
        config=config,
    )
    first = runner.run_once().summary()
    # 第二轮模拟「重启后重扫」：换一个新 Runner，结果应全部是 AlreadyIngested、没有新写入。
    rescan = Runner(
        source=runner.source,
        storage=runner.storage,
        locator=runner.locator,
        config=config,
    ).run_once()
    print(json.dumps({"first": first, "rescan": rescan.summary()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
