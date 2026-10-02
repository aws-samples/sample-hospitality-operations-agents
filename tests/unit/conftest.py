# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from __future__ import annotations

import sys
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parent
CONSOLE_LAYER = UNIT.parents[1] / "infra" / "lambdas" / "console" / "layer" / "python"
for path in (UNIT, CONSOLE_LAYER):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import staff_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_staff_registry(monkeypatch):
    """Replace the registry lookup for every test; start each one empty."""
    import hotel_console.api as api

    staff_registry.REGISTRY.clear()
    monkeypatch.setattr(api, "registered_scope", lambda sub: staff_registry.REGISTRY.get(sub))
    yield
    staff_registry.REGISTRY.clear()
