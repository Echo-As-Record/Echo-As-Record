"""Deployment composition, defaulting to the source-pinned Octopus HTTP adapter.

Set EAR_GATEWAY_STORAGE_TOKEN and the four EAR_OCTOPUS_* settings documented in
ear_llm_gateway.octopus.create_provider. Custom --provider-factory remains supported.
No production wire verification, no model fallback, and no body capture by default.
Reevaluation uses the caller-token storage preflight; old storage fails closed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os

import uvicorn
from ear_storage_client.remote import RemoteStorageClient

from ear_llm_gateway.client import endpoint
from ear_llm_gateway.http import create_app
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.service import Gateway


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-url", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--provider-factory", default="ear_llm_gateway.octopus:create_provider")
    parser.add_argument("--aliases", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    writer = journal = None
    try:
        storage_url = str(endpoint(args.storage_url))
        aliases = json.loads(args.aliases)
        if not isinstance(aliases, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in aliases.items()
        ):
            raise ValueError
        module, name = args.provider_factory.split(":", 1)
        provider = getattr(importlib.import_module(module), name)()
        writer = RemoteStorageClient(
            os.environ["EAR_GATEWAY_STORAGE_TOKEN"], storage_url, module="llm_gateway"
        )
        journal = Journal(args.journal)
        gateway = Gateway(
            callers=lambda token: RemoteStorageClient(token, storage_url),
            writer=writer,
            provider=provider,
            journal=journal,
            aliases=aliases,
        )
        # Recover audits before accepting NEW work. A failing ① keeps the
        # deployment unready, rather than quietly accumulating missing records.
        while gateway.recover_batch(limit=100).processed:
            pass
        uvicorn.run(
            create_app(gateway),
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            log_level="warning",
        )
    except Exception:
        # No config/URL/token/import/adapter exception values in stderr.
        parser.exit(
            1, "gateway startup/runtime failed; inspect deployment health without secrets\n"
        )
    finally:
        if writer is not None:
            writer.close()
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    main()
