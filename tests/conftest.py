# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test configuration.

Puts the Lambda layer's ``python/`` directory on ``sys.path`` so tests import
``hotel_ops`` exactly the way the deployed functions do, rather than through a
copy that could drift.

No AWS credentials are needed or used by anything under ``tests/unit``.
``tests/integration`` is a script, not a pytest suite, precisely so a bare
``pytest`` run can never touch the live foundation.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAYER_PYTHON = REPO_ROOT / "tools" / "layer" / "python"

for path in (str(LAYER_PYTHON), str(REPO_ROOT / "infra")):
    if path not in sys.path:
        sys.path.insert(0, path)
