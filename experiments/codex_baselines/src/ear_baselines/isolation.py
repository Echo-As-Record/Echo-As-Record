"""Executed inside the check container, using exactly the exam mounts and environment."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


def internet() -> bool:
    """经 HTTPS_PROXY（若有）连 api.openai.com 并完成 TLS；拿到任何 HTTP 状态都算通。"""
    proxy = os.environ.get("HTTPS_PROXY")
    try:
        if proxy:
            url = urlsplit(proxy)
            conn = http.client.HTTPSConnection(str(url.hostname), url.port, timeout=15)
            conn.set_tunnel("api.openai.com", 443)
        else:
            conn = http.client.HTTPSConnection("api.openai.com", 443, timeout=15)
        conn.request("HEAD", "/v1/models")
        conn.getresponse()
        conn.close()
        return True
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return False


def check(ports: list[int], extra_hosts: list[str] | None = None) -> dict:
    hosts = ["host.docker.internal", "host.lima.internal", "192.168.5.2", *(extra_hosts or [])]
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == "00000000":
            hosts.append(socket.inet_ntoa(bytes.fromhex(fields[2])[::-1]))
    result: dict = {
        "paths_absent": not any(p.exists() for p in [Path("/Users"), Path.home() / ".octopus"]),
        "environment_clean": not any(k.startswith("EAR_ADAPTER") for k in os.environ),
        "ports": ports,
        "tcp": [],
    }
    for port in ports:
        for host in dict.fromkeys(hosts):
            try:
                with socket.create_connection((host, port), timeout=3):
                    reachable = True
                reason = "connected"
            except OSError as exc:
                reachable, reason = False, type(exc).__name__
            result["tcp"].append(
                {"host": host, "port": port, "reachable": reachable, "reason": reason}
            )
    result["internet_tls"] = internet()
    result["ok"] = (
        result["paths_absent"]
        and result["environment_clean"]
        and result["internet_tls"]
        and not any(r["reachable"] for r in result["tcp"])
    )
    return result


if __name__ == "__main__":
    args = [*sys.argv[1:], "", ""]
    result = check([int(p) for p in args[0].split(",") if p], [h for h in args[1].split(",") if h])
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)
