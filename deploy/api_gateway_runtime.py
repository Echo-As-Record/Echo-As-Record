"""File-mounted gateway composition; never initialize identities or schemas.

Operator-provisioned files must be regular, single-link, UID 10001 owned and 0600.
Local Compose file secrets preserve host ownership/mode; uid/gid options cannot fix it.
Provider credentials, if required by the configured factory, must also be mounted files.
The factory and its credentials are deployment trusted, never caller controlled.
Direct mode requires a gateway-only key and a read-only gateway mount of the
shared model-settings directory (not its individual, atomically replaced file).
The bootstrap alias is never used instead of the required startup snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys

DIRECT_MODEL_SETTINGS = "/run/config/model-settings/settings.json"
DIRECT_TOKEN = "/run/secrets/newapi-token"
DIRECT_FACTORY_NAME = "_create_direct_provider"


def protected_file(path: str, limit: int = 65536, *, exact_mode: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o077
            or (exact_mode and stat.S_IMODE(before.st_mode) != 0o600)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise ValueError("protected file required")
        content = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(content) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("protected file changed")
        return content


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration")
        result[key] = value
    return result


def direct_aliases() -> dict[str, str]:
    from ear_llm_gateway.openai_compatible import valid_model_id
    from ear_llm_gateway.output_json import strict_json

    projection = strict_json(protected_file(DIRECT_MODEL_SETTINGS, 4096, exact_mode=True))
    if (
        not isinstance(projection, dict)
        or set(projection) != {"model", "revision"}
        or type(projection["revision"]) is not int
        or not 1 <= projection["revision"] <= (1 << 63) - 1
        or not valid_model_id(projection["model"])
    ):
        raise ValueError("invalid model settings")
    return {"default": projection["model"]}


def _create_direct_provider():
    from ear_llm_gateway.openai_compatible import OpenAICompatibleProvider

    token = protected_file(DIRECT_TOKEN, 8196, exact_mode=True).decode("ascii")
    # HTTP Core's debug traces include untrusted response headers/exception text.
    # Limit library logging in this dedicated runtime, even under root DEBUG.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return OpenAICompatibleProvider(
        token.removesuffix("\r\n") if token.endswith("\r\n") else token.removesuffix("\n")
    )


def configure_provider(config):
    if "openai_compatible" in config:
        if (
            config["openai_compatible"] is not True
            or {"octopus", "provider_factory"} & config.keys()
            or config.get("aliases") != {"default": "gpt-6-luna"}
        ):
            raise ValueError("invalid direct provider configuration")
        # The entrypoint is already imported as __main__ when run as a script.
        # Existing CLI imports this deployment factory; the key stays out of argv/env.
        return f"{__name__}:{DIRECT_FACTORY_NAME}"
    factory = config.get("provider_factory", "ear_llm_gateway.octopus:create_provider")
    if not isinstance(factory, str) or factory in {
        f"{module}:{DIRECT_FACTORY_NAME}"
        for module in (__name__, "__main__", "api_gateway_runtime", "deploy.api_gateway_runtime")
    }:
        raise ValueError("invalid provider factory")
    if factory == "ear_llm_gateway.octopus:create_provider":
        options = config.get("octopus")
        if not isinstance(options, dict) or set(options) != {"origin", "models", "service"}:
            raise ValueError("invalid Octopus configuration")
        if not all(isinstance(options[k], str) for k in ("origin", "service")):
            raise ValueError("invalid Octopus configuration")
        if not isinstance(options["models"], dict):
            raise ValueError("invalid Octopus models")
        # Fixed secret path: never accept credentials from JSON, args or Docker Env.
        credential = protected_file("/run/secrets/octopus-token", 8194).decode("ascii").strip()
        os.environ["EAR_OCTOPUS_ORIGIN"] = options["origin"]
        os.environ["EAR_OCTOPUS_MODELS"] = json.dumps(options["models"], allow_nan=False)
        os.environ["EAR_OCTOPUS_SERVICE"] = options["service"]
        os.environ["EAR_OCTOPUS_TOKEN"] = credential
    elif "octopus" in config:
        raise ValueError("ambiguous provider configuration")
    return factory


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise ValueError("no runtime initialization commands")
        config = json.loads(
            protected_file(
                os.environ.get("EAR_GATEWAY_CONFIG", "/run/secrets/gateway-config.json")
            ),
            object_pairs_hook=unique_object,
        )
        if (
            not isinstance(config, dict)
            or not {"storage_url", "aliases"} <= set(config)
            or set(config)
            - {
                "storage_url",
                "aliases",
                "provider_factory",
                "octopus",
                "openai_compatible",
                "provider_timeout_seconds",
            }
        ):
            raise ValueError("invalid configuration")
        if not isinstance(config["storage_url"], str):
            raise ValueError("invalid configuration")
        from ear_llm_gateway.models import MAX_PROVIDER_TIMEOUT_SECONDS

        timeout = config.get("provider_timeout_seconds", 60)
        if type(timeout) is not int or not 1 <= timeout <= MAX_PROVIDER_TIMEOUT_SECONDS:
            raise ValueError("invalid provider timeout")
        factory = configure_provider(config)
        # Snapshot the required projection once. The bootstrap alias is not a fallback;
        # later edits are read only in a newly started gateway.
        aliases = direct_aliases() if "openai_compatible" in config else config["aliases"]
        token = protected_file("/run/secrets/gateway-storage-token", 128).decode("ascii").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise ValueError("invalid credential")
        # Process-private only: never in Docker environment, command arguments or logs.
        os.environ["EAR_GATEWAY_STORAGE_TOKEN"] = token
        from ear_llm_gateway.cli import main as gateway_main

        sys.argv = [
            "ear-llm-gateway",
            "--storage-url",
            config["storage_url"],
            "--provider-factory",
            factory,
            "--aliases",
            json.dumps(aliases, allow_nan=False),
            "--provider-timeout-seconds",
            str(timeout),
            "--journal",
            "/state/gateway.sqlite3",
            "--host",
            "0.0.0.0",
            "--port",
            "8077",
        ]
        gateway_main()
        return 0
    except (Exception, SystemExit):
        print('{"code":"gateway_container_unavailable"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
