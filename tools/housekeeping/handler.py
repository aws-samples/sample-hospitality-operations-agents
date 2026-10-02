# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway target ``housekeeping`` -- A2, Housekeeping Flow (Tier 1).

Signs in as ``agent-housekeeping+{propertyId}@…`` (Cognito group **Housekeeping**).

Why every tool here takes ``propertyId``
----------------------------------------
``Housekeeping`` is in neither ``CHAIN_LEVEL_GROUPS`` nor ``REGIONAL_GROUPS``, so
``verify_property_access`` denies a token whose ``custom:property_id`` is unset.
This agent therefore has one Cognito user per property, and ``propertyId`` selects
which identity the call signs in as -- it is not a filter, it is the identity.

A useful consequence, verified live: because ``get_property_id(event)`` takes
precedence over the ``propertyId`` query parameter in ``list_tasks.py`` and
``room_status.py``, a property-scoped token is pinned server-side. Passing
another property's id returns this property's tasks, not theirs. The tenancy
boundary does not depend on this Lambda behaving.

This agent's group cannot reach billing at all: ``post_charge`` requires
Manager/Admin, so the same token 403s there. Verified.

The task state machine (``003_pms_schema.sql``)::

    PENDING -> ASSIGNED -> CLEANING -> COMPLETED -> INSPECTING -> INSPECTED
                                                             \\-> DIRTY (failed)

``assign`` accepts only PENDING/ASSIGNED, ``complete`` only ASSIGNED/CLEANING,
``inspect`` only INSPECTING. Anything else is a ``409 INVALID_STATE`` -- which is
information for the model, not an error to retry.
"""

from __future__ import annotations

import os

from hotel_ops.foundation_client import FoundationClient
from hotel_ops.tool_dispatch import ToolRouter

router = ToolRouter("housekeeping")
client = FoundationClient()

DEFAULT_LIMIT = int(os.environ.get("DEFAULT_PAGE_LIMIT", "100"))


@router.tool("list_tasks")
def list_tasks(args: dict) -> dict:
    """Housekeeping tasks at one property, HIGH priority first then oldest.

    The foundation orders by ``priority`` then ``created_at``; the agent's job is
    to re-sequence that into an efficient route (same-floor batching), not to
    re-sort it by priority again.
    """
    property_id = args["propertyId"]
    return client.get(
        "pms",
        "/housekeeping/tasks",
        query={
            "status": args.get("status"),
            "priority": args.get("priority"),
            "assignedTo": args.get("assignedTo"),
            "page": args.get("page"),
            "limit": args.get("limit", DEFAULT_LIMIT),
        },
        property_id=property_id,
    )["data"]


@router.tool("get_task")
def get_task(args: dict) -> dict:
    """One task in full, including its room number and current status."""
    return client.get(
        "pms",
        f"/housekeeping/tasks/{args['taskId']}",
        property_id=args["propertyId"],
    )["data"]


@router.tool("room_board")
def room_board(args: dict) -> dict:
    """Room status board for one property: counts plus every room's floor.

    ``floor`` is the field that makes same-floor batching possible, and it is the
    only room attribute beyond number/type/status that any endpoint exposes.
    """
    return client.get(
        "pms",
        "/housekeeping/rooms/summary",
        property_id=args["propertyId"],
    )["data"]


@router.tool("assign_task")
def assign_task(args: dict) -> dict:
    """Assign a PENDING or ASSIGNED task to a named housekeeper.

    ``assignedTo`` is free text (``VARCHAR(100)``). The platform has no staff
    roster, no skills, and no shift data, so the agent can only reassign among
    names it has already seen in ``list_tasks`` output. It must not invent staff.
    """
    return client.put(
        "pms",
        f"/housekeeping/tasks/{args['taskId']}/assign",
        body={"assignedTo": args["assignedTo"]},
        property_id=args["propertyId"],
    )["data"]


@router.tool("complete_task")
def complete_task(args: dict) -> dict:
    """Mark cleaning complete. Advances the Step Functions workflow."""
    body: dict = {}
    if args.get("notes"):
        body["notes"] = args["notes"]
    return client.post(
        "pms",
        f"/housekeeping/tasks/{args['taskId']}/complete",
        body=body,
        property_id=args["propertyId"],
    )["data"]


@router.tool("inspect_task")
def inspect_task(args: dict) -> dict:
    """Pass or fail inspection on a task in INSPECTING.

    ``passed=false`` sends the room back to DIRTY and re-opens cleaning, so it is
    a real operational decision, not a formality.
    """
    body: dict = {"passed": bool(args["passed"])}
    if args.get("notes"):
        body["notes"] = args["notes"]
    return client.post(
        "pms",
        f"/housekeeping/tasks/{args['taskId']}/inspect",
        body=body,
        property_id=args["propertyId"],
    )["data"]


def handler(event, context):
    return router.dispatch(event, context)
