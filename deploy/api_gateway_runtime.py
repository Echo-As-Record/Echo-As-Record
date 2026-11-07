"""File-mounted gateway composition; never initialize identities or schemas.

Operator-provisioned files must be regular, single-link, UID 10001 owned and 0600.
Local Compose file secrets preserve host ownership/mode; uid/gid options cannot fix it.
Provider credentials, if required by the configured factory, must also be mounted files.
The factory and its credentials are deployment trusted, never caller controlled.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys


def protected_file(path: str, limit: int = 65536) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o077
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


def configure_provider(config):
    factory = config.get("provider_factory", "ear_llm_gateway.octopus:create_provider")
    if not isinstance(factory, str):
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
            or set(config) - {"storage_url", "aliases", "provider_factory", "octopus"}
        ):
            raise ValueError("invalid configuration")
        if not isinstance(config["storage_url"], str):
            raise ValueError("invalid configuration")
        factory = configure_provider(config)
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
            json.dumps(config["aliases"], allow_nan=False),
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
