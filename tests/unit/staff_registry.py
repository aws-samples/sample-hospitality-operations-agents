# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A stand-in for ``hotel-ops-agent-staff-scope``, shared by the console tests.

The console now takes every caller's scope from that registry, never from the token's
own claims. Most console tests are about something else, so their event builders call
:func:`register_as_claimed` -- "an administrator registered this caller with exactly
the scope their token shows" -- and the registry agrees. The tests that are *about* the
registry write to :data:`REGISTRY` directly, or leave a caller out of it.
"""

from __future__ import annotations

REGISTRY: dict[str, dict] = {}


def register_as_claimed(claims: dict) -> None:
    REGISTRY[claims["sub"]] = {
        "sub": claims["sub"],
        "propertyId": claims.get("custom:property_id") or None,
        "region": claims.get("custom:region") or None,
    }
