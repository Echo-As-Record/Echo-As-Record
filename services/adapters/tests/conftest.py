"""测试夹具。合成数据和测试替身在 ear_adapter.testing。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from ear_adapter import AdapterConfig
from ear_adapter.testing import evidence_json


@pytest.fixture
def config() -> AdapterConfig:
    return AdapterConfig()


@pytest.fixture
def make_evidence() -> Callable[..., dict[str, Any]]:
    def make(run: str = "run-1") -> dict[str, Any]:
        return copy.deepcopy(evidence_json(run))

    return make
