# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A4 -- Night Audit Readiness. Tier 3, advise-only."""

from __future__ import annotations

from strands import tool

from subagents.base import delegate

KEY = "night_audit"
TARGET = "nightaudit"


@tool
async def night_audit_agent(request: str) -> str:
    """Night-audit readiness and metric agreement for one property-day.

    Delegate here for: whether a property is ready to close the day, what will
    break if the audit runs now, stays still checked in past their checkout date,
    unsettled folios blocking the close, rooms stuck mid-state, and whether the
    live daily figures agree with the stored audit snapshot.

    It reads only. Triggering the audit is Admin-only and stays with a human, so
    this agent produces a readiness verdict and a list of exceptions with
    identifiers -- never a completed fix.

    It is also the agent that catches the platform's own reporting
    inconsistencies: the live and stored endpoints name the same metrics
    differently, and both publish an `adr` computed from a different denominator.
    It reports those as findings with both values rather than picking one.

    Scope is one property and one date. For multi-day or cross-property trends,
    use the regional agent instead.

    Args:
        request: The property and date to assess, plus anything specific you want
            checked.
    """
    return await delegate(key=KEY, target=TARGET, request=request)
