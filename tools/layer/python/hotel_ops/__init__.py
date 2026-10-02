# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared runtime for the AgentCore Gateway Lambda targets.

Packaged as a Lambda layer, so it lives under ``python/`` as Lambda requires.
Two modules:

* :mod:`hotel_ops.foundation_client` -- authenticated HTTP to the CRS/PMS APIs.
* :mod:`hotel_ops.tool_dispatch` -- MCP tool-name routing for a Gateway target.
"""

from hotel_ops.foundation_client import (
    FoundationClient,
    FoundationError,
    is_conflict,
)
from hotel_ops.tool_dispatch import ToolRouter, strip_target_prefix, tool_name_from

__all__ = [
    "FoundationClient",
    "FoundationError",
    "ToolRouter",
    "is_conflict",
    "strip_target_prefix",
    "tool_name_from",
]
