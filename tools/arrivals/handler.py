# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway target ``arrivals`` -- A1, Arrivals & Room Assignment (Tier 1).

Signs in as ``agent-arrivals@…`` (Cognito group **Manager**, a chain-level group,
so ``custom:property_id`` is unset and every tool takes ``propertyId`` explicitly).

What A1 can actually reason over
--------------------------------
Verified against the deployed APIs, not inferred from the schema. The foundation
stores richer guest data than it exposes; these are the fields an agent can
genuinely read:

===========================================  =========================================
Reachable                                    Source
===========================================  =========================================
``loyaltyTier`` per arrival                  ``GET /stays``
tier, points, totalStays, staysToNextTier    ``GET /loyalty/{guestId}``
room ``floor``, ``status``, ``roomNumber``    ``GET /housekeeping/rooms/summary``
``accessibilityType``, ``smokingAllowed``,   ``GET /properties/{id}/room-types`` (CRS)
``bedConfiguration``, ``maxOccupancy``
===========================================  =========================================

Deliberately absent, because **no endpoint returns them**:
``guests.preferences`` and ``guests.vip_level``/``tags`` (CRS ``/guests/{id}`` is
owner-only and 403s for staff; no handler in the platform selects ``vip_level``
or ``tags`` at all), ``reservations.special_requests`` (CRS
``/reservations/{id}`` is owner-only), and ``rooms.wing`` / ``features`` /
``is_connecting`` / ``connecting_room_id`` (selected by no handler anywhere).

So A1 does tier-aware, accessibility-aware, type-fit, floor- and status-aware
assignment. It must not claim to match stated guest preferences -- there is no
preference data on the wire to match against. The system prompt says so too;
this docstring is the reason why.
"""

from __future__ import annotations

import os
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from hotel_ops.foundation_client import FoundationClient
from hotel_ops.tool_dispatch import ToolRouter, tool_error

router = ToolRouter("arrivals")
client = FoundationClient()

#: Statuses that mean the room is not assignable right now. Everything else is
#: reported to the model with its status attached so it can decide.
UNASSIGNABLE_ROOM_STATUSES = {"OCCUPIED", "OUT_OF_ORDER", "OUT_OF_INVENTORY"}

DEFAULT_LIMIT = int(os.environ.get("DEFAULT_PAGE_LIMIT", "50"))

#: Days past the operating date that ``list_arrivals`` covers by default. One,
#: not zero: A1's job is *pre-arrival* assignment, and by the time a guest is
#: standing at the desk the naive check-in-time picker has already chosen. Today
#: alone is also frequently empty -- the platform checks arrivals in as they
#: happen, so today's arrivals stop being CONFIRMED as the day progresses.
DEFAULT_ARRIVAL_WINDOW_DAYS = 1

#: Widening the window costs a page walk, and an agent that pulls a month of
#: reservations into context to assign tomorrow's rooms is reasoning worse, not
#: better.
MAX_ARRIVAL_WINDOW_DAYS = 14

#: Pages walked looking for the window. ``GET /stays`` sorts by check_in_date
#: ascending, so the window is always in the first pages; this bound exists so a
#: property with a large backlog of stale CONFIRMED rows cannot turn one tool
#: call into a hundred API calls.
MAX_PAGES = 5


@router.tool("list_arrivals")
def list_arrivals(args: dict) -> dict:
    """Reservations *arriving* within a date window, earliest check-in first.

    The foundation cannot express "arriving on date D", which is why this tool
    does more than forward its arguments. ``GET /stays?date=D`` filters
    ``check_in_date <= D AND check_out_date >= D`` -- in house on D. So asking it
    for CONFIRMED stays on today's date returns only reservations that have
    already begun and never checked in, and *excludes every arrival still ahead
    of you*: exactly the opposite of what A1 needs. Passing today's date is also
    the obvious thing for a model to do, and it silently returns zero rows, which
    reads as "no arrivals" rather than "wrong question".

    So the window is applied here, on ``checkInDate``, over pages the API returns
    in ascending check-in order:

    * ``date`` is the first day of the window and defaults to today (UTC, like
      the foundation's own reporting endpoints).
    * ``daysAhead`` extends it; 0 means that single day.

    The reply states the window it used and how many rows it scanned to fill it,
    because "no arrivals" and "no arrivals in the day I happened to look at" are
    different answers and the model cannot tell them apart otherwise.
    """
    property_id = args["propertyId"]
    start = (args.get("date") or "").strip() or _today()
    _validate_date(start)

    days = _window_days(args.get("daysAhead"))
    if isinstance(days, dict):
        return days
    end = (date_type.fromisoformat(start) + timedelta(days=days)).isoformat()

    status = args.get("status", "CONFIRMED")
    limit = args.get("limit", DEFAULT_LIMIT)

    arriving: list[dict] = []
    scanned = 0
    total = None
    pages_walked = 0
    reached_end_of_window = False

    for page in range(1, MAX_PAGES + 1):
        result = client.get(
            "pms",
            "/stays",
            query={
                "propertyId": property_id,
                # CONFIRMED = booked but not yet checked in, i.e. an arrival.
                "status": status,
                # `date` is deliberately NOT forwarded: see the docstring. The
                # window is applied below, on checkInDate.
                "page": page,
                "limit": limit,
            },
        )
        if not result["ok"]:
            # §4.3: the foundation's error envelope reaches the model verbatim.
            return result["data"]

        payload = (result["data"] or {}).get("data") or {}
        stays = payload.get("stays") or []
        pagination = payload.get("pagination") or {}
        total = pagination.get("total", total)
        scanned += len(stays)
        pages_walked = page

        arriving.extend(s for s in stays if start <= (s.get("checkInDate") or "") <= end)
        # Ascending order means the first row past the window ends the walk.
        if any((s.get("checkInDate") or "") > end for s in stays):
            reached_end_of_window = True
            break
        if not stays or page >= (pagination.get("totalPages") or page):
            reached_end_of_window = True
            break

    unassigned = [s for s in arriving if not s.get("roomId")]
    return {
        "success": True,
        "data": {
            "propertyId": property_id,
            "window": {
                "from": start,
                "to": end,
                "meaning": (
                    f"reservations whose checkInDate falls between {start} and "
                    f"{end} inclusive"
                ),
            },
            "status": status,
            "counts": {
                "arriving": len(arriving),
                "unassigned": len(unassigned),
                "scanned": scanned,
                f"total{status.title().replace('_', '')}": total,
            },
            # Named `stays` because that is the foundation's own key and each item
            # is its row, verbatim.
            "stays": arriving,
            "windowComplete": reached_end_of_window,
            "note": _window_note(
                arriving, start, end, reached_end_of_window, pages_walked, total
            ),
        },
    }


def _today() -> str:
    """UTC, matching the foundation's date handling. A local-time default would
    query the wrong day for part of the chain."""
    return datetime.now(timezone.utc).date().isoformat()


def _validate_date(value: str) -> None:
    # Raised, not returned: the router turns ValueError into INVALID_ARGUMENT with
    # this message attached, which is what the model needs to correct itself.
    date_type.fromisoformat(value)


def _window_days(raw: object) -> int | dict:
    """The window width, or an error envelope explaining the bound."""
    if raw is None or raw == "":
        return DEFAULT_ARRIVAL_WINDOW_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return tool_error(
            "INVALID_ARGUMENT",
            f"daysAhead must be a whole number of days, got {raw!r}.",
        )
    if not 0 <= days <= MAX_ARRIVAL_WINDOW_DAYS:
        return tool_error(
            "INVALID_ARGUMENT",
            f"daysAhead must be between 0 and {MAX_ARRIVAL_WINDOW_DAYS}, got {days}. "
            "Assign rooms for the arrivals you can actually reason about; a wider "
            "window is a worse decision, not a bigger one.",
            daysAhead=days,
            maximum=MAX_ARRIVAL_WINDOW_DAYS,
        )
    return days


def _window_note(
    arriving: list[dict],
    start: str,
    end: str,
    complete: bool,
    pages: int,
    total: int | None,
) -> str:
    if not complete:
        return (
            f"Stopped after {pages} pages without reaching the end of the window. "
            f"There may be further arrivals between {start} and {end}; narrow the "
            "window or raise limit."
        )
    if arriving:
        return ""
    if total:
        return (
            f"No arrivals between {start} and {end}, though this property has "
            f"{total} reservations in that status overall. They are further out: "
            "widen daysAhead to see them. Do not report this as 'no reservations'."
        )
    return f"No reservations in this status at this property between {start} and {end}."


@router.tool("get_loyalty_profile")
def get_loyalty_profile(args: dict) -> dict:
    """Loyalty standing for one guest: tier, points, stays to next tier."""
    return client.get("pms", f"/loyalty/{args['guestId']}")["data"]


@router.tool("list_rooms")
def list_rooms(args: dict) -> dict:
    """Rooms at one property, each enriched with its room type's attributes.

    Two calls, because the room inventory and the room-type attributes live on
    different APIs and neither returns the other's fields:

    * ``GET /housekeeping/rooms/summary`` (PMS) -- roomId, roomNumber, floor,
      status, and the room type's *name*.
    * ``GET /properties/{id}/room-types`` (CRS) -- accessibilityType,
      smokingAllowed, bedConfiguration, maxOccupancy, amenities, squareFeet.

    The join is on the room-type **name**, because the rooms summary does not
    return ``roomTypeId``. Names are unique per property in practice; a room
    whose type name does not match is still returned, with
    ``roomTypeUnresolved: true``, rather than silently dropped.
    """
    property_id = args["propertyId"]

    rooms_result = client.get(
        "pms", "/housekeeping/rooms/summary", query={"propertyId": property_id}
    )
    if not rooms_result["ok"]:
        return rooms_result["data"]

    types_result = client.get("crs", f"/properties/{property_id}/room-types")
    if not types_result["ok"]:
        return types_result["data"]

    by_name = {
        rt["name"]: {
            "roomTypeId": rt.get("roomTypeId"),
            "code": rt.get("code"),
            "maxOccupancy": rt.get("maxOccupancy"),
            "bedConfiguration": rt.get("bedConfiguration"),
            "accessibilityType": rt.get("accessibilityType"),
            "smokingAllowed": rt.get("smokingAllowed"),
            "squareFeet": rt.get("squareFeet"),
            "amenities": rt.get("amenities"),
        }
        for rt in (types_result["data"].get("data") or [])
    }

    summary = rooms_result["data"]["data"]
    rooms = []
    for room in summary.get("rooms", []):
        attributes = by_name.get(room.get("roomType"))
        enriched = dict(room)
        if attributes:
            enriched.update(attributes)
        else:
            enriched["roomTypeUnresolved"] = True
        enriched["assignable"] = room.get("status") not in UNASSIGNABLE_ROOM_STATUSES
        rooms.append(enriched)

    only_assignable = args.get("onlyAssignable", False)
    if only_assignable:
        rooms = [r for r in rooms if r["assignable"]]

    return {
        "success": True,
        "data": {
            "propertyId": property_id,
            "totalRooms": summary.get("totalRooms"),
            "occupancyPercent": summary.get("occupancyPercent"),
            "statusCounts": {
                key: summary.get(key)
                for key in (
                    "available",
                    "occupied",
                    "dirty",
                    "cleaning",
                    "inspecting",
                    "outOfOrder",
                )
            },
            "rooms": rooms,
        },
    }


@router.tool("assign_room")
def assign_room(args: dict) -> dict:
    """Pre-assign a room to a CONFIRMED reservation. Tier 1, auto-execute.

    ``reason`` is required by this tool but not by the foundation: it is what the
    decision log records, and what a human compares against when judging whether
    the agent chose well.

    Read-then-write, per §4.3. A ``409 ALREADY_ASSIGNED`` is reported as success
    by someone else -- a front-desk human getting there first is a correct
    outcome, not a failure and not a retry trigger.
    """
    reservation_id = args["reservationId"]
    reason = (args.get("reason") or "").strip()
    if not reason:
        return tool_error(
            "MISSING_ARGUMENT",
            "reason is required: it is written to the decision log so a human can "
            "audit why this room was chosen.",
        )

    refusal = _room_in_scope(args["propertyId"], args["roomId"])
    if refusal:
        return refusal

    result = client.put(
        "pms",
        f"/stays/{reservation_id}/assign-room",
        body={"roomId": args["roomId"]},
    )

    if result["status"] == 409:
        envelope = result["data"] if isinstance(result["data"], dict) else {}
        code = (envelope.get("error") or {}).get("code")
        if code == "ALREADY_ASSIGNED":
            return {
                "success": True,
                "data": {
                    "reservationId": reservation_id,
                    "outcome": "ALREADY_ASSIGNED_BY_SOMEONE_ELSE",
                    "note": (
                        "A room was already assigned to this reservation, most "
                        "likely by front-desk staff. Nothing was changed. Do not "
                        "retry."
                    ),
                },
                "metadata": {"foundationResponse": envelope},
            }

    return result["data"]


@router.tool("check_in")
def check_in(args: dict) -> dict:
    """Check a guest in, optionally into a specific room.

    Kept separate from ``assign_room`` because check-in is same-day and
    irreversible-ish, whereas pre-assignment is freely revisable before arrival.

    Both the reservation and any named room are proven to be at ``propertyId``
    first. The room check matters more here than in ``assign_room``: the
    platform's check-in accepts a specific ``roomId`` without checking which
    property that room is at.
    """
    refusal = _reservation_arriving_at(args["propertyId"], args["reservationId"])
    if refusal:
        return refusal
    if args.get("roomId"):
        refusal = _room_in_scope(args["propertyId"], args["roomId"])
        if refusal:
            return refusal

    body: dict = {}
    if args.get("roomId"):
        body["roomId"] = args["roomId"]
    if args.get("notes"):
        body["notes"] = args["notes"]
    return client.post(
        "pms", f"/stays/{args['reservationId']}/checkin", body=body
    )["data"]


# --------------------------------------------------------------------------- #
# Scope: this identity is chain-level, so nothing upstream proves which hotel a
# reservation or room is at. These do, before any write.
# --------------------------------------------------------------------------- #

#: Pages walked looking for one arriving reservation. Arrivals are sorted by
#: check-in date, so the walk stops at the first row past the window; this bounds a
#: property with an unusually long list of overdue arrivals.
MAX_SCOPE_PAGES = 10


def _room_in_scope(property_id: str, room_id: str) -> dict | None:
    """Refuse unless ``room_id`` is one of ``property_id``'s rooms.

    For a pre-assignment this is enough to prove the *reservation* is there too: the
    platform's ``assign-room`` only accepts a room at the reservation's own
    property (``pre_assign_room.py`` looks the room up by
    ``room_id AND property_id = reservation.property_id``). So a room proven to be at
    this run's property can only be assigned to a reservation at this run's
    property.
    """
    result = client.get(
        "pms", "/housekeeping/rooms/summary", query={"propertyId": property_id}
    )
    if not result["ok"]:
        return result["data"]
    rooms = (((result["data"] or {}).get("data")) or {}).get("rooms") or []
    if any(room.get("roomId") == room_id for room in rooms):
        return None
    return tool_error(
        "OUT_OF_SCOPE",
        f"Room {room_id} is not at property {property_id}. Only rooms at this run's "
        "property can be assigned.",
        roomId=room_id,
    )


def _reservation_arriving_at(property_id: str, reservation_id: str) -> dict | None:
    """Refuse unless ``reservation_id`` is a CONFIRMED arrival at ``property_id``.

    The platform has no staff endpoint that reads one reservation (``GET
    /reservations/{id}`` is guest-owner-only), so this walks the property's
    CONFIRMED stays -- the same list ``list_arrivals`` reads -- up to a day past
    today. A check-in that is not in that list is either at another hotel or not
    due, and neither should happen on an agent's say-so.
    """
    horizon = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    for page in range(1, MAX_SCOPE_PAGES + 1):
        result = client.get(
            "pms",
            "/stays",
            query={
                "propertyId": property_id,
                "status": "CONFIRMED",
                "page": page,
                "limit": 100,
            },
        )
        if not result["ok"]:
            return result["data"]
        payload = (result["data"] or {}).get("data") or {}
        stays = payload.get("stays") or []
        if any(stay.get("reservationId") == reservation_id for stay in stays):
            return None
        pagination = payload.get("pagination") or {}
        if (
            not stays
            or any((stay.get("checkInDate") or "") > horizon for stay in stays)
            or page >= (pagination.get("totalPages") or page)
        ):
            break
    return tool_error(
        "OUT_OF_SCOPE",
        f"Reservation {reservation_id} is not a confirmed arrival at property "
        f"{property_id} due by {horizon}. Only this run's property's arrivals can be "
        "checked in.",
        reservationId=reservation_id,
    )


def handler(event, context):
    return router.dispatch(event, context)
