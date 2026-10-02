# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A5 -- Regional Performance. Tier 3, advise-only. The only agent with code execution.

A5 is the one sub-agent that gets a tool beyond its Gateway target.
``range_metrics`` returns up to 92 days per property and accepts ``_all`` for the
whole chain, so a single regional question can put thousands of daily figures in
front of the model. Asking it to compute variance, trendlines, and RevPAR deltas
from that by reading is precisely the failure mode Code Interpreter exists to
prevent -- and the failure is silent, because a confidently-wrong average looks
exactly like a right one.

A1 through A4 do not get it. None of them reasons over a series: they read the
current state of one property, decide, and act.
"""

from __future__ import annotations

from strands import tool

from code_execution import run_analysis
from subagents.base import delegate

KEY = "regional"
TARGET = "regional"


@tool
async def regional_agent(request: str) -> str:
    """Cross-property and multi-day performance analysis.

    Delegate here for: comparing properties against each other or against their
    own history, occupancy and revenue trends over a date range, RevPAR and ADR
    movement, ranking properties within a region, explaining an outlier, and the
    list of properties in the chain.

    It is the only agent with code execution, and it uses it: variance, trends,
    and RevPAR arithmetic over up to 92 days across dozens of properties are
    computed rather than estimated.

    It is advisory by necessity, not policy -- this platform has no endpoint that
    writes a rate or an availability restriction, so there is nothing for it to
    execute. It also has no data on rate plans, competitors, market demand,
    channel mix, group business, or guest satisfaction, and will say so rather
    than guess.

    For a single property on a single day, use the night audit agent instead.

    Args:
        request: What comparison or trend you want, with the date range and the
            properties or region of interest.
    """
    return await delegate(
        key=KEY,
        target=TARGET,
        request=request,
        # A callable, not a list, because base.py builds extra tools inside the
        # worker thread. This one is cheap and stateless -- the sandbox is opened
        # lazily on first use -- but the contract is the same for both.
        extra_tools=lambda: [run_analysis],
    )
