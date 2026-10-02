# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A2 -- Housekeeping Flow. Tier 1, auto-execute. One property per run."""

from __future__ import annotations

from strands import tool

from subagents.base import delegate

KEY = "housekeeping"
TARGET = "housekeeping"


@tool
async def housekeeping_agent(request: str) -> str:
    """Cleaning task sequencing, assignment, completion, and inspection.

    Delegate here for: what order housekeeping should work in, who should clean
    what, advancing a task through the workflow, passing or failing an
    inspection, the room status board, and rooms stuck mid-clean.

    It sequences on task priority, task type, the room's floor for batching, and
    the platform's PENDING -> ASSIGNED -> CLEANING -> COMPLETED -> INSPECTING ->
    INSPECTED state machine. It may only assign work to housekeeper names that
    already appear in task data, because the platform has no staff roster, no
    skills data, and no shift schedule.

    It moves tasks without approval. It cannot assign rooms to reservations,
    touch folios or loyalty, or set a room's status directly -- no such endpoint
    exists.

    **This agent works on exactly one property and cannot run chain-wide.** Its
    foundation credentials are scoped to a single property, so the run must carry
    a property scope or it will refuse.

    Args:
        request: What you want sequenced, assigned, or advanced, naming the
            property and any task IDs, room numbers, or housekeeper names you
            already know.
    """
    return await delegate(key=KEY, target=TARGET, request=request)
