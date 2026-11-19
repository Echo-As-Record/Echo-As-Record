"""All package-selected Docker builds still need every workspace manifest."""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.api", "Dockerfile.gateway"])
def test_docker_build_copies_all_workspace_manifests_before_sync(name):
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
    dockerfile = (ROOT / "deploy" / name).read_text()
    before_sync = dockerfile.split("RUN uv sync", 1)[0]
    assert "RUN uv sync" in dockerfile
    for member in workspace["members"]:
        manifest = f"{member}/pyproject.toml"
        assert (ROOT / manifest).is_file()
        assert f"COPY {manifest} {manifest}" in before_sync, (name, manifest)
