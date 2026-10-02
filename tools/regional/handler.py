# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway target ``regional`` -- A5, Regional Performance (Tier 3, advise-only).

Signs in as ``agent-regional@…`` (Cognito group **RegionalManager**, with
``custom:region`` unset so it sees every region).

**Read-only by construction, and permanently so.** The foundation exposes no
write endpoint for rates, inventory, or availability -- ``PATTERN_EXTENSION_GUIDE.md`` §6
explains why that makes A5 advise-only rather than absent. A5's output is
always advice for a human revenue manager; there is nothing here for it to
execute even if it wanted to.

A5 is the one agent wired to **Code Interpreter**. ``GET /reporting/range``
returns up to 92 days x N properties; variance, trendlines, and RevPAR deltas
computed by an LLM reading raw JSON in context is precisely the arithmetic that
Code Interpreter exists to do correctly instead.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from hotel_ops.foundation_client import FoundationClient
from hotel_ops.tool_dispatch import ToolRouter

router = ToolRouter("regional")
client = FoundationClient()

#: ``GET /reporting/range`` rejects anything wider. Enforced here too so the
#: agent gets a usable message instead of a 400 it has to interpret.
MAX_RANGE_DAYS = 92

#: ``propertyId`` value that means "every property", per ``range_metrics.py``.
CHAIN_WIDE = "_all"


@router.tool("list_properties")
def list_properties(_args: dict) -> dict:
    """Every active property with its city, state, and region.

    Unpaginated, and a chain-level caller gets all of them. This is the map A5
    works from -- ``region`` here is what groups properties for comparison.
    """
    return client.get("pms", "/properties")["data"]


@router.tool("occupancy")
def occupancy(args: dict) -> dict:
    """Every property's *current* occupancy, plus one day's revenue and movements.

    Despite the endpoint's name and its two date parameters, this is not a range
    report, and the reply says so rather than letting the model assume otherwise.
    Read against ``pms/reporting/occupancy_report.py``:

    * ``occupiedRooms`` and ``occupancyPercent`` count ``rooms.status = 'OCCUPIED'``
      right now. **No date is involved at all.**
    * ``revenueToday``, ``checkInsToday`` and ``checkOutsToday`` filter on
      ``= endDate`` -- a single day, not a span.
    * ``startDate`` is accepted and then **used by no query in the handler.**

    So three different historical windows return byte-identical data, which A5
    noticed on its second run and reported as a data-quality finding. It was right.
    The parameters are forwarded anyway -- suppressing them would be a second lie,
    and ``endDate`` genuinely selects the revenue day -- but the ``meaning`` block
    below travels with the numbers so nobody reads a seven-day request as a
    seven-day answer.

    There is no ``propertyId`` filter. This is the only tool that returns a row per
    property across the whole chain; ``range_metrics`` with ``_all`` aggregates the
    chain into one series instead.
    """
    start = args.get("startDate")
    end = args.get("endDate")
    result = client.get(
        "pms",
        "/reporting/occupancy",
        query={"startDate": start, "endDate": end, "region": args.get("region")},
    )
    if not result["ok"]:
        return result["data"]

    payload = result["data"]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload["data"]["meaning"] = {
            "occupancyPercent": (
                "Live room status, as of now. Not affected by startDate or endDate."
            ),
            "revenueToday": (
                f"ROOM_RATE charges on {end or 'today'} only -- one day, not a range."
            ),
            "checkInsToday": f"Check-ins recorded on {end or 'today'} only.",
            "startDate": (
                "Echoed back but unused by the foundation. A range request returns "
                "the same numbers as a single-day one; do not describe this as a "
                "trend or a period total."
            ),
            "forHistory": (
                "Use range_metrics per property. There is no endpoint that returns "
                "per-property history in one call."
            ),
        }
    return payload


@router.tool("range_metrics")
def range_metrics(args: dict) -> dict:
    """Per-day metrics for one property, or the whole chain with ``_all``.

    Defaults to the trailing 30 days ending today, matching the foundation's own
    default, so the agent can ask for a trend without inventing dates.
    """
    end_raw = args.get("endDate")
    start_raw = args.get("startDate")

    end = _parse_date(end_raw, "endDate") if end_raw else date.today()
    start = (
        _parse_date(start_raw, "startDate")
        if start_raw
        else end - timedelta(days=29)
    )

    if start > end:
        raise ValueError(f"startDate {start} is after endDate {end}")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValueError(
            f"Requested {span} days; the endpoint caps a range at "
            f"{MAX_RANGE_DAYS}. Split the comparison into shorter windows."
        )

    return client.get(
        "pms",
        "/reporting/range",
        query={
            "propertyId": args.get("propertyId", CHAIN_WIDE),
            "startDate": str(start),
            "endDate": str(end),
        },
    )["data"]


@router.tool("daily_report")
def daily_report(args: dict) -> dict:
    """One property-day in detail, to explain an outlier the range view surfaced."""
    return client.get(
        "pms",
        f"/reporting/{args['propertyId']}/daily",
        query={"date": args.get("date")},
    )["data"]


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}")


def handler(event, context):
    return router.dispatch(event, context)
