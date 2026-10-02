# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A1 -- Arrivals & Room Assignment. Tier 1, auto-execute."""

from __future__ import annotations

from strands import tool

from subagents.base import delegate

KEY = "arrivals"
TARGET = "arrivals"


@tool
async def arrivals_agent(request: str) -> str:
    """Room assignment, arrivals, and room inventory for one property.

    Delegate here for: which room an arriving guest should get, pre-assigning
    rooms, checking a guest in, who is arriving today, what rooms exist and
    which are assignable, and a guest's loyalty standing as it bears on the room
    they should get.

    This agent replaces the platform's own room assignment, which simply takes
    the highest floor and lowest room number. It assigns on loyalty tier,
    accessibility requirement, bed configuration, room-type fit, occupancy
    ceiling, floor, and current room status -- the signals the platform actually
    exposes. It has no access to stated guest preferences, VIP flags, guest
    tags, special requests, or arrival times, because no endpoint returns them.

    It pre-assigns rooms without approval. It cannot touch folios, loyalty
    balances, or housekeeping tasks.

    Args:
        request: What you want decided or done, naming the property and any
            reservation IDs, guest names, or room numbers you already know.
    """
    return await delegate(key=KEY, target=TARGET, request=request)
