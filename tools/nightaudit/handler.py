# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway target ``nightaudit`` -- A4, Night Audit Readiness (Tier 3, advise-only).

Signs in as ``agent-nightaudit@…`` (Cognito group **Manager**, chain-level).

**This target is read-only by construction.** It exposes no write tool at all.
``POST /audit/runs`` is Admin-only and stays human-triggered: ``PATTERN_EXTENSION_GUIDE.md``
§3.1 is explicit that no agent gets ``Admin``. A4's product is an exception
report a human reads before they press the button.

The metric inconsistency A4 must surface, not paper over
-------------------------------------------------------
The two endpoints A4 reads report the same day's numbers under different names,
and one metric under the same name with a *different definition*. Verified in
``night_audit/worker.py`` and ``reporting/daily_summary.py``:

======================  ==============================  ==========================
Concept                 ``/audit/reports/{id}``         ``/reporting/{id}/daily``
                        (``metrics.*``)
======================  ==============================  ==========================
rooms occupied          ``occupiedRooms``               ``occupancy.occupied``
total rooms             ``totalRooms``                  ``occupancy.totalRooms``
occupancy percent       ``occupancyPercent``            ``occupancy.percent``
room revenue            ``dailyRevenue``                ``revenue``
rooms sold              ``roomsSold``                   ``roomsSold``
**ADR**                 ``dailyRevenue / occupiedRooms``  ``revenue / roomsSold``
======================  ==============================  ==========================

The last row is the one that bites: both are called ``adr`` and they disagree
whenever occupied rooms differ from rooms sold. :func:`compare_metrics` computes
the delta explicitly so A4 reports the discrepancy as a finding instead of
silently trusting whichever value it happened to read first.
"""

from __future__ import annotations

import os

from hotel_ops.foundation_client import FoundationClient
from hotel_ops.tool_dispatch import ToolRouter

router = ToolRouter("nightaudit")
client = FoundationClient()

DEFAULT_LIMIT = int(os.environ.get("DEFAULT_PAGE_LIMIT", "100"))


@router.tool("daily_report")
def daily_report(args: dict) -> dict:
    """Operational summary for one property-day, computed live from the tables."""
    return client.get(
        "pms",
        f"/reporting/{args['propertyId']}/daily",
        query={"date": args.get("date")},
    )["data"]


@router.tool("audit_report")
def audit_report(args: dict) -> dict:
    """The stored night-audit run for one property-day.

    A ``404 NOT_FOUND`` means the audit has not run for that date yet -- which for
    A4 is the normal pre-audit state and a useful signal, not an error.
    """
    return client.get(
        "pms",
        f"/audit/reports/{args['propertyId']}",
        query={"date": args.get("date")},
    )["data"]


@router.tool("compare_metrics")
def compare_metrics(args: dict) -> dict:
    """Read both reports for one property-day and diff the overlapping metrics.

    Returns the two source values for each concept plus a ``discrepancies`` list.
    Reporting the disagreement is the point; A4 must not silently pick a side.
    """
    property_id = args["propertyId"]
    date = args.get("date")

    daily = client.get(
        "pms", f"/reporting/{property_id}/daily", query={"date": date}
    )
    audit = client.get(
        "pms", f"/audit/reports/{property_id}", query={"date": date}
    )

    if not daily["ok"]:
        return daily["data"]

    daily_data = daily["data"]["data"]
    occupancy = daily_data.get("occupancy") or {}

    if not audit["ok"]:
        return {
            "success": True,
            "data": {
                "propertyId": property_id,
                "date": daily_data.get("date"),
                "auditReportAvailable": False,
                "auditReportStatus": audit["status"],
                "note": (
                    "No stored night-audit run for this date, so there is nothing "
                    "to compare against. This is the expected pre-audit state."
                ),
                "dailyReport": daily_data,
            },
        }

    metrics = audit["data"]["data"].get("metrics") or {}

    # (label, value from /audit/reports, value from /reporting/daily)
    pairs = [
        ("totalRooms", metrics.get("totalRooms"), occupancy.get("totalRooms")),
        ("roomsOccupied", metrics.get("occupiedRooms"), occupancy.get("occupied")),
        ("occupancyPercent", metrics.get("occupancyPercent"), occupancy.get("percent")),
        ("roomRevenue", metrics.get("dailyRevenue"), daily_data.get("revenue")),
        ("roomsSold", metrics.get("roomsSold"), daily_data.get("roomsSold")),
        ("adr", metrics.get("adr"), daily_data.get("adr")),
    ]

    comparison = {}
    discrepancies = []
    for label, from_audit, from_daily in pairs:
        comparison[label] = {"auditReport": from_audit, "dailyReport": from_daily}
        if from_audit is None or from_daily is None:
            continue
        if from_audit != from_daily:
            entry = {
                "metric": label,
                "auditReport": from_audit,
                "dailyReport": from_daily,
            }
            if label == "adr":
                entry["cause"] = (
                    "Not a data error: the two endpoints define ADR differently. "
                    "/audit/reports divides room revenue by rooms *occupied*; "
                    "/reporting/daily divides it by rooms *sold*. They diverge "
                    "whenever those two counts differ."
                )
            discrepancies.append(entry)

    return {
        "success": True,
        "data": {
            "propertyId": property_id,
            "date": daily_data.get("date"),
            "auditReportAvailable": True,
            "comparison": comparison,
            "discrepancies": discrepancies,
        },
    }


@router.tool("list_stays")
def list_stays(args: dict) -> dict:
    """Stays at one property -- used to find stays that should have checked out.

    A stay still ``CHECKED_IN`` with a ``checkOutDate`` in the past is exactly the
    kind of exception the audit needs cleared first.
    """
    return client.get(
        "pms",
        "/stays",
        query={
            "propertyId": args["propertyId"],
            "status": args.get("status"),
            "date": args.get("date"),
            "page": args.get("page"),
            "limit": args.get("limit", DEFAULT_LIMIT),
        },
    )["data"]


@router.tool("list_folios")
def list_folios(args: dict) -> dict:
    """Folios at one property -- used to find unsettled balances before audit."""
    return client.get(
        "pms",
        "/billing/folios",
        query={
            "propertyId": args["propertyId"],
            "status": args.get("status"),
            "page": args.get("page"),
            "limit": args.get("limit", DEFAULT_LIMIT),
        },
    )["data"]


@router.tool("room_board")
def room_board(args: dict) -> dict:
    """Room status counts -- finds rooms stuck mid-state before the audit runs."""
    return client.get(
        "pms", "/housekeeping/rooms/summary", query={"propertyId": args["propertyId"]}
    )["data"]


def handler(event, context):
    return router.dispatch(event, context)
