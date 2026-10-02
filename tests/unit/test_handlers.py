# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The five tool handlers, with the foundation replaced by a scripted fake.

These cover the behaviours a live integration run cannot safely provoke: what
``assign_room`` does when the foundation says ``409 ALREADY_ASSIGNED``, what
``compare_metrics`` reports when the two reporting endpoints genuinely disagree,
and every way the billing approval gate can be asked to fail open.

The last group is the most important set of tests in the repository. The rule
from ``hotel-operations-agent.md`` §7 is that an agent may reorganize work freely
and must never move money on its own; ``require_approval`` is where that rule is
mechanically true, so each of its failure modes is asserted individually rather
than as one happy-path check.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The handlers construct a FoundationClient at module scope, which reads these.
# Set before import; every one is replaced by a fake before any test runs.
os.environ.setdefault("CRS_API_URL", "https://crs.test")
os.environ.setdefault("PMS_API_URL", "https://pms.test")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_TEST")
os.environ.setdefault("ADMIN_AUTH_CLIENT_ID", "client")
os.environ.setdefault("AGENT_CREDS_SECRET_ARN", "arn:test")
os.environ.setdefault("AGENT_USERNAME_TEMPLATE", "agent@test")


def load(target: str):
    """Import ``tools/<target>/handler.py`` under a unique module name."""
    name = f"_handler_{target}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "tools" / target / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeFoundation:
    """Scripted responses keyed on ``"<METHOD> <path>"``, with call recording."""

    def __init__(self, routes: dict[str, tuple[int, dict]]):
        self.routes = routes
        self.calls: list[dict] = []

    def call(self, method, surface, path, *, body=None, query=None, property_id=None):
        self.calls.append(
            {
                "method": method,
                "surface": surface,
                "path": path,
                "body": body,
                "query": {k: v for k, v in (query or {}).items() if v is not None},
                "property_id": property_id,
            }
        )
        key = f"{method} {path}"
        if key not in self.routes:
            raise AssertionError(f"unscripted call {key!r}; scripted: {list(self.routes)}")
        status, data = self.routes[key]
        return {"status": status, "data": data, "ok": 200 <= status < 300}

    def get(self, surface, path, **kw):
        return self.call("GET", surface, path, **kw)

    def post(self, surface, path, **kw):
        return self.call("POST", surface, path, **kw)

    def put(self, surface, path, **kw):
        return self.call("PUT", surface, path, **kw)


def wire(monkeypatch, target: str, routes: dict) -> tuple[object, FakeFoundation]:
    module = load(target)
    fake = FakeFoundation(routes)
    monkeypatch.setattr(module, "client", fake)
    return module, fake


def ok(data: dict) -> dict:
    return {"success": True, "data": data}


# --------------------------------------------------------------------------- #
# A1 arrivals
# --------------------------------------------------------------------------- #

ROOM_SUMMARY = ok(
    {
        "totalRooms": 3,
        "occupancyPercent": 33.3,
        "available": 2,
        "occupied": 1,
        "dirty": 0,
        "cleaning": 0,
        "inspecting": 0,
        "outOfOrder": 0,
        "rooms": [
            {"roomId": "r1", "roomNumber": "101", "floor": 1, "roomType": "Standard King", "status": "AVAILABLE"},
            {"roomId": "r2", "roomNumber": "205", "floor": 2, "roomType": "Accessible Queen", "status": "AVAILABLE"},
            {"roomId": "r3", "roomNumber": "301", "floor": 3, "roomType": "Standard King", "status": "OCCUPIED"},
            {"roomId": "r4", "roomNumber": "401", "floor": 4, "roomType": "Renamed Suite", "status": "AVAILABLE"},
        ],
    }
)

#: One CONFIRMED stay, arriving today, at property p1.
ARRIVING_RES1 = ok(
    {
        "stays": [{"reservationId": "res1", "checkInDate": "2000-01-01", "roomId": None}],
        "pagination": {"totalPages": 1, "total": 1},
    }
)

ROOM_TYPES = ok(
    [
        {
            "roomTypeId": "t1", "name": "Standard King", "code": "STD-K",
            "maxOccupancy": 2, "bedConfiguration": "1 King",
            "accessibilityType": None, "smokingAllowed": False,
            "squareFeet": 325, "amenities": ["WiFi"],
        },
        {
            "roomTypeId": "t2", "name": "Accessible Queen", "code": "ACC-Q",
            "maxOccupancy": 2, "bedConfiguration": "1 Queen",
            "accessibilityType": "MOBILITY", "smokingAllowed": False,
            "squareFeet": 340, "amenities": ["WiFi", "Roll-in Shower"],
        },
    ]
)


def test_list_rooms_joins_two_apis_on_room_type_name(monkeypatch):
    module, fake = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "GET /properties/p1/room-types": (200, ROOM_TYPES),
        },
    )
    result = module.list_rooms({"propertyId": "p1"})
    rooms = {r["roomNumber"]: r for r in result["data"]["rooms"]}

    # The join is on name, because the rooms summary returns no roomTypeId.
    assert rooms["205"]["accessibilityType"] == "MOBILITY"
    assert rooms["205"]["bedConfiguration"] == "1 Queen"
    assert [c["surface"] for c in fake.calls] == ["pms", "crs"]


def test_list_rooms_marks_an_unresolvable_room_type_rather_than_dropping_it(monkeypatch):
    """Silently dropping a room would make A1 blind to real inventory."""
    module, _ = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "GET /properties/p1/room-types": (200, ROOM_TYPES),
        },
    )
    rooms = {r["roomNumber"]: r for r in module.list_rooms({"propertyId": "p1"})["data"]["rooms"]}
    assert len(rooms) == 4
    assert rooms["401"]["roomTypeUnresolved"] is True
    assert "roomTypeUnresolved" not in rooms["101"]


@pytest.mark.parametrize(
    "status,assignable",
    [
        ("AVAILABLE", True),
        ("DIRTY", True),        # cleanable before arrival, so still a candidate
        ("INSPECTING", True),
        ("OCCUPIED", False),
        ("OUT_OF_ORDER", False),
        ("OUT_OF_INVENTORY", False),
    ],
)
def test_assignability_is_derived_from_room_status(monkeypatch, status, assignable):
    summary = json.loads(json.dumps(ROOM_SUMMARY))
    summary["data"]["rooms"] = [
        {"roomId": "r", "roomNumber": "1", "floor": 1, "roomType": "Standard King", "status": status}
    ]
    module, _ = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, summary),
            "GET /properties/p1/room-types": (200, ROOM_TYPES),
        },
    )
    assert module.list_rooms({"propertyId": "p1"})["data"]["rooms"][0]["assignable"] is assignable


def test_only_assignable_filters_but_is_off_by_default(monkeypatch):
    routes = {
        "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
        "GET /properties/p1/room-types": (200, ROOM_TYPES),
    }
    module, _ = wire(monkeypatch, "arrivals", routes)
    assert len(module.list_rooms({"propertyId": "p1"})["data"]["rooms"]) == 4
    assert len(
        module.list_rooms({"propertyId": "p1", "onlyAssignable": True})["data"]["rooms"]
    ) == 3


def test_list_rooms_surfaces_an_upstream_error_instead_of_a_partial_join(monkeypatch):
    error = {"success": False, "error": {"code": "FORBIDDEN", "message": "denied"}}
    module, _ = wire(
        monkeypatch,
        "arrivals",
        {"GET /housekeeping/rooms/summary": (403, error)},
    )
    assert module.list_rooms({"propertyId": "p1"}) == error


def test_assign_room_reports_a_409_conflict_as_success_by_someone_else(monkeypatch):
    """§4.3. A front-desk human getting there first is a correct outcome."""
    conflict = {
        "success": False,
        "error": {"code": "ALREADY_ASSIGNED", "message": "Room already assigned"},
    }
    module, _ = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "PUT /stays/res1/assign-room": (409, conflict),
        },
    )
    result = module.assign_room(
        {"propertyId": "p1", "reservationId": "res1", "roomId": "r1", "reason": "tier fit"}
    )
    assert result["success"] is True
    assert result["data"]["outcome"] == "ALREADY_ASSIGNED_BY_SOMEONE_ELSE"
    assert "Do not retry" in result["data"]["note"]
    # The real response is still attached, so nothing is hidden from the model.
    assert result["metadata"]["foundationResponse"] == conflict


def test_a_409_for_a_different_reason_is_not_laundered_into_success(monkeypatch):
    """Only ALREADY_ASSIGNED is benign; a state conflict must stay an error."""
    other = {"success": False, "error": {"code": "INVALID_STATE", "message": "checked out"}}
    module, _ = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "PUT /stays/res1/assign-room": (409, other),
        },
    )
    assert module.assign_room(
        {"propertyId": "p1", "reservationId": "res1", "roomId": "r1", "reason": "x"}
    ) == other


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_assign_room_requires_a_real_reason_for_the_decision_log(monkeypatch, reason):
    module, fake = wire(monkeypatch, "arrivals", {})
    args = {"reservationId": "res1", "roomId": "r1"}
    if reason is not None:
        args["reason"] = reason
    result = module.assign_room(args)
    assert result["error"]["code"] == "MISSING_ARGUMENT"
    assert fake.calls == [], "must refuse before writing, not after"


# --- list_arrivals: the window --------------------------------------------- #
#
# The foundation cannot express "arriving on date D". `GET /stays?date=D` filters
# `check_in_date <= D AND check_out_date >= D` -- in house on D -- so passing
# today's date returns only reservations that already began and never checked in,
# and hides every arrival still ahead of you. That is not a hypothetical: the
# shipped version forwarded `date` and answered "zero arrivals" at a property with
# 513 CONFIRMED reservations. These tests pin the window semantics that replaced
# it.

TODAY = "2026-09-08"
TOMORROW = "2026-09-09"


class StaysPages(FakeFoundation):
    """``GET /stays`` served as real pages, because ``list_arrivals`` paginates.

    ``FakeFoundation`` keys on method and path alone, so every page of the same
    request would return the same rows and the walk would never terminate. This
    fake reads ``query["page"]`` the way the foundation does.
    """

    def __init__(self, pages: list[list[dict]], total: int | None = None):
        super().__init__({})
        self.pages = pages
        self.total = total if total is not None else sum(len(p) for p in pages)

    def call(self, method, surface, path, *, body=None, query=None, property_id=None):
        query = {k: v for k, v in (query or {}).items() if v is not None}
        self.calls.append(
            {
                "method": method,
                "surface": surface,
                "path": path,
                "body": body,
                "query": query,
                "property_id": property_id,
            }
        )
        assert (method, path) == ("GET", "/stays"), f"unexpected {method} {path}"
        page = query.get("page", 1)
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return {
            "status": 200,
            "ok": True,
            "data": ok(
                {
                    "stays": rows,
                    "pagination": {
                        "page": page,
                        "total": self.total,
                        "totalPages": len(self.pages),
                    },
                }
            ),
        }


def arrival(reservation_id: str, check_in: str, *, room_id=None) -> dict:
    return {
        "reservationId": reservation_id,
        "checkInDate": check_in,
        "checkOutDate": "2026-09-30",
        "roomId": room_id,
        "loyaltyTier": "GOLD",
    }


def arrivals_with(monkeypatch, pages, total=None, today=TODAY):
    module = load("arrivals")
    fake = StaysPages(pages, total)
    monkeypatch.setattr(module, "client", fake)
    monkeypatch.setattr(module, "_today", lambda: today)
    return module, fake


def test_list_arrivals_filters_on_check_in_date_and_never_forwards_date(monkeypatch):
    """The bug this replaced, asserted from both ends."""
    module, fake = arrivals_with(
        monkeypatch,
        [
            [
                arrival("yesterday", "2026-09-07"),  # already began: not an arrival
                arrival("res-today", TODAY),
                arrival("res-tomorrow", TOMORROW),
                arrival("res-next-week", "2026-09-15"),
            ]
        ],
    )
    result = module.list_arrivals({"propertyId": "p1"})

    assert result["success"] is True
    data = result["data"]
    assert [s["reservationId"] for s in data["stays"]] == ["res-today", "res-tomorrow"]
    assert data["window"] == {
        "from": TODAY,
        "to": TOMORROW,
        "meaning": (
            f"reservations whose checkInDate falls between {TODAY} and {TOMORROW} "
            "inclusive"
        ),
    }
    assert data["counts"]["arriving"] == 2
    assert data["counts"]["scanned"] == 4
    # The whole point: `date` must not reach the foundation, or its in-house
    # filter silently excludes the arrivals we came for.
    assert "date" not in fake.calls[0]["query"]
    assert fake.calls[0]["query"]["status"] == "CONFIRMED"


def test_days_ahead_zero_is_a_single_day(monkeypatch):
    module, _ = arrivals_with(
        monkeypatch, [[arrival("a", TODAY), arrival("b", TOMORROW)]]
    )
    result = module.list_arrivals({"propertyId": "p1", "daysAhead": 0})
    assert result["data"]["window"] == {
        "from": TODAY,
        "to": TODAY,
        "meaning": (
            f"reservations whose checkInDate falls between {TODAY} and {TODAY} inclusive"
        ),
    }
    assert [s["reservationId"] for s in result["data"]["stays"]] == ["a"]


def test_an_explicit_date_starts_the_window_there(monkeypatch):
    module, _ = arrivals_with(
        monkeypatch, [[arrival("a", TODAY), arrival("b", "2026-09-20")]]
    )
    result = module.list_arrivals(
        {"propertyId": "p1", "date": "2026-09-20", "daysAhead": 0}
    )
    assert [s["reservationId"] for s in result["data"]["stays"]] == ["b"]


def test_unassigned_arrivals_are_counted_because_they_are_the_work(monkeypatch):
    module, _ = arrivals_with(
        monkeypatch,
        [[arrival("a", TODAY), arrival("b", TODAY, room_id="r9"), arrival("c", TOMORROW)]],
    )
    counts = module.list_arrivals({"propertyId": "p1"})["data"]["counts"]
    assert counts == {
        "arriving": 3,
        "unassigned": 2,
        "scanned": 3,
        "totalConfirmed": 3,
    }


def test_the_page_walk_stops_at_the_first_row_past_the_window(monkeypatch):
    """``GET /stays`` sorts by check_in_date ascending, so one row past the end
    proves there is nothing left to find. Walking further would be pure cost."""
    module, fake = arrivals_with(
        monkeypatch,
        [
            [arrival("a", TODAY)],
            [arrival("b", TOMORROW), arrival("c", "2026-10-01")],
            [arrival("d", "2026-10-02")],
        ],
    )
    result = module.list_arrivals({"propertyId": "p1"})
    assert [s["reservationId"] for s in result["data"]["stays"]] == ["a", "b"]
    assert len(fake.calls) == 2, "walked past the end of the window"
    assert result["data"]["windowComplete"] is True


def test_the_walk_is_bounded_and_says_so_rather_than_implying_completeness(monkeypatch):
    pages = [[arrival(f"r{n}", TODAY)] for n in range(8)]
    module, fake = arrivals_with(monkeypatch, pages)
    result = module.list_arrivals({"propertyId": "p1"})
    assert len(fake.calls) == module.MAX_PAGES < len(pages)
    assert result["data"]["windowComplete"] is False
    assert "Stopped after" in result["data"]["note"]


def test_an_empty_window_says_the_reservations_are_further_out(monkeypatch):
    """Otherwise the model reports "this property has no reservations", which is
    what it did before the window existed, and it was wrong."""
    module, _ = arrivals_with(monkeypatch, [[arrival("far", "2026-11-01")]])
    data = module.list_arrivals({"propertyId": "p1"})["data"]
    assert data["counts"]["arriving"] == 0
    assert "1 reservations in that status overall" in data["note"]
    assert "widen daysAhead" in data["note"]


def test_an_empty_property_is_reported_as_empty_without_the_widen_advice(monkeypatch):
    module, _ = arrivals_with(monkeypatch, [[]], total=0)
    data = module.list_arrivals({"propertyId": "p1"})["data"]
    assert data["counts"]["arriving"] == 0
    assert "widen" not in data["note"]


@pytest.mark.parametrize("days", [-1, 15, 400])
def test_days_ahead_is_bounded_before_any_api_call(monkeypatch, days):
    module, fake = arrivals_with(monkeypatch, [[arrival("a", TODAY)]])
    result = module.list_arrivals({"propertyId": "p1", "daysAhead": days})
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    assert fake.calls == [], "refused after paying for the read"


def test_a_non_numeric_days_ahead_is_refused_not_coerced(monkeypatch):
    module, _ = arrivals_with(monkeypatch, [[arrival("a", TODAY)]])
    result = module.list_arrivals({"propertyId": "p1", "daysAhead": "a week"})
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_a_malformed_date_fails_with_a_message_the_model_can_act_on(monkeypatch):
    module, fake = arrivals_with(monkeypatch, [[arrival("a", TODAY)]])
    with pytest.raises(ValueError):
        module.list_arrivals({"propertyId": "p1", "date": "next tuesday"})
    assert fake.calls == []


def test_list_arrivals_returns_an_upstream_error_envelope_verbatim(monkeypatch):
    """§4.3. The window logic must not swallow a 403 into an empty result."""
    error = {"success": False, "error": {"code": "FORBIDDEN", "message": "denied"}}
    module, _ = wire(monkeypatch, "arrivals", {"GET /stays": (403, error)})
    monkeypatch.setattr(module, "_today", lambda: TODAY)
    assert module.list_arrivals({"propertyId": "p1"}) == error


def test_check_in_omits_optional_fields_rather_than_sending_nulls(monkeypatch):
    module, fake = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /stays": (200, ARRIVING_RES1),
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "POST /stays/res1/checkin": (200, ok({})),
        },
    )
    posts = lambda: [c["body"] for c in fake.calls if c["method"] == "POST"]  # noqa: E731
    module.check_in({"propertyId": "p1", "reservationId": "res1"})
    assert posts() == [{}]
    module.check_in(
        {"propertyId": "p1", "reservationId": "res1", "roomId": "r1", "notes": "late"}
    )
    assert posts()[1] == {"roomId": "r1", "notes": "late"}


# --------------------------------------------------------------------------- #
# A2 housekeeping
# --------------------------------------------------------------------------- #


def test_every_housekeeping_tool_passes_property_id_as_the_identity(monkeypatch):
    """propertyId selects the Cognito user here; it is not a query filter."""
    module, fake = wire(
        monkeypatch,
        "housekeeping",
        {
            "GET /housekeeping/tasks": (200, ok({"tasks": []})),
            "GET /housekeeping/tasks/t1": (200, ok({})),
            "GET /housekeeping/rooms/summary": (200, ok({})),
            "PUT /housekeeping/tasks/t1/assign": (200, ok({})),
            "POST /housekeeping/tasks/t1/complete": (200, ok({})),
            "POST /housekeeping/tasks/t1/inspect": (200, ok({})),
        },
    )
    module.list_tasks({"propertyId": "p1"})
    module.get_task({"propertyId": "p1", "taskId": "t1"})
    module.room_board({"propertyId": "p1"})
    module.assign_task({"propertyId": "p1", "taskId": "t1", "assignedTo": "Ana"})
    module.complete_task({"propertyId": "p1", "taskId": "t1"})
    module.inspect_task({"propertyId": "p1", "taskId": "t1", "passed": True})

    assert len(fake.calls) == 6
    assert all(c["property_id"] == "p1" for c in fake.calls)
    # And it is never *also* sent as a query param, which the foundation ignores.
    assert all("propertyId" not in c["query"] for c in fake.calls)


def test_inspect_task_coerces_passed_to_a_real_boolean(monkeypatch):
    module, fake = wire(
        monkeypatch,
        "housekeeping",
        {"POST /housekeeping/tasks/t1/inspect": (200, ok({}))},
    )
    module.inspect_task({"propertyId": "p1", "taskId": "t1", "passed": "false"})
    # A non-empty string is truthy; the model must send a JSON boolean. What
    # matters is that the wire value is a bool, never the raw string.
    assert isinstance(fake.calls[0]["body"]["passed"], bool)


def test_a_missing_property_id_raises_key_error_which_dispatch_maps_cleanly(monkeypatch):
    module, _ = wire(monkeypatch, "housekeeping", {})
    with pytest.raises(KeyError, match="propertyId"):
        module.list_tasks({})


# --------------------------------------------------------------------------- #
# A3 billing -- the approval gate
# --------------------------------------------------------------------------- #


class FakeDynamo:
    def __init__(self, item=None, error: Exception | None = None):
        self.item = item
        self.error = error
        self.calls: list[dict] = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Item": self.item} if self.item else {}


def approval(status="APPROVED", action="post_charge", **bindings):
    """An approval record, optionally bound to a target and an amount.

    ``bindings`` mirror what the ops console writes: ``folioId``, ``guestId``,
    ``amount``. Numbers become ``N`` attributes so the handler's float comparison is
    exercised on the shape DynamoDB actually returns.
    """
    item = {"status": {"S": status}}
    if action is not None:
        item["action"] = {"S": action}
    for key, value in bindings.items():
        item[key] = (
            {"N": str(value)} if isinstance(value, (int, float)) else {"S": str(value)}
        )
    return item


def wire_billing(monkeypatch, *, table="approvals", dynamo=None, routes=None):
    module, fake = wire(monkeypatch, "billing", routes or {})
    monkeypatch.setattr(module, "APPROVALS_TABLE", table)
    monkeypatch.setattr(module, "_approvals", lambda: dynamo or FakeDynamo())
    return module, fake


PAID_ROUTES = {
    # The ownership read every folio write makes first.
    "GET /billing/folios/f1": (200, ok({"folioId": "f1", "propertyId": "p1"})),
    "POST /billing/folios/f1/charges": (200, ok({"chargeId": "c1"})),
    "POST /billing/folios/f1/void": (200, ok({"folioId": "f1"})),
    "POST /loyalty/g1/adjust": (200, ok({"guestId": "g1"})),
}

TIER_2 = [
    ("post_charge", {"propertyId": "p1", "folioId": "f1", "description": "x", "amount": 10}),
    ("void_folio", {"propertyId": "p1", "folioId": "f1", "reason": "x"}),
    ("adjust_loyalty", {"guestId": "g1", "points": 100, "reason": "x"}),
]


#: The approval the console would have recorded for each TIER_2 call -- every
#: captured argument, which is what both gates now bind.
MATCHING = {
    "post_charge": dict(folioId="f1", amount=10, description="x"),
    "void_folio": dict(folioId="f1", voidReason="x"),
    "adjust_loyalty": dict(guestId="g1", points=100, adjustReason="x"),
}


@pytest.mark.parametrize("tool,args", TIER_2, ids=[t for t, _ in TIER_2])
def test_no_tier_2_write_happens_without_an_approval_token(monkeypatch, tool, args):
    module, fake = wire_billing(monkeypatch, routes=PAID_ROUTES)
    result = getattr(module, tool)(args)
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert fake.calls == [], "the foundation must never be called"


@pytest.mark.parametrize("tool,args", TIER_2, ids=[t for t, _ in TIER_2])
def test_a_valid_approval_lets_the_write_through(monkeypatch, tool, args):
    dynamo = FakeDynamo(approval(action=tool, **MATCHING[tool]))
    module, fake = wire_billing(monkeypatch, dynamo=dynamo, routes=PAID_ROUTES)
    result = getattr(module, tool)({**args, "approval_token": "tok-1"})
    assert result["success"] is True
    assert len([c for c in fake.calls if c["method"] == "POST"]) == 1
    # Read consistently: a token approved a moment ago must not read as absent.
    assert dynamo.calls[0]["ConsistentRead"] is True
    assert dynamo.calls[0]["Key"] == {"approvalId": {"S": "tok-1"}}


@pytest.mark.parametrize(
    "dynamo,expected_code",
    [
        (FakeDynamo(None), "APPROVAL_INVALID"),
        (FakeDynamo(approval(status="PENDING")), "APPROVAL_INVALID"),
        (FakeDynamo(approval(status="REJECTED")), "APPROVAL_INVALID"),
        (FakeDynamo(approval(action="void_folio")), "APPROVAL_MISMATCH"),
        (FakeDynamo(error=RuntimeError("ResourceNotFoundException")), "APPROVAL_UNVERIFIABLE"),
    ],
    ids=["absent", "pending", "rejected", "granted-for-another-action", "table-unreachable"],
)
def test_the_gate_fails_closed_in_every_direction(monkeypatch, dynamo, expected_code):
    module, fake = wire_billing(monkeypatch, dynamo=dynamo, routes=PAID_ROUTES)
    result = module.post_charge(
        {"folioId": "f1", "description": "x", "amount": 10, "approval_token": "tok"}
    )
    assert result["error"]["code"] == expected_code
    assert fake.calls == []


def test_an_unconfigured_approvals_table_never_reads_as_approved(monkeypatch):
    """Inverting this one check would silently disable the whole guardrail."""
    module, fake = wire_billing(monkeypatch, table="", routes=PAID_ROUTES)
    result = module.post_charge(
        {"folioId": "f1", "description": "x", "amount": 10, "approval_token": "tok"}
    )
    assert result["error"]["code"] == "APPROVAL_UNVERIFIABLE"
    assert fake.calls == []


def test_an_approval_with_no_recorded_action_authorizes_nothing(monkeypatch):
    """This test once asserted the opposite: that a record with no action released
    any action, on the grounds that a human had approved *something*. A security
    review rejected that reading -- the console always records the action, so a record
    without one is not what it claims to be -- and the gate now refuses it."""
    dynamo = FakeDynamo(approval(action=None, folioId="f1", voidReason="x"))
    module, fake = wire_billing(monkeypatch, dynamo=dynamo, routes=PAID_ROUTES)
    result = module.void_folio(
        {"propertyId": "p1", "folioId": "f1", "reason": "x", "approval_token": "t"}
    )
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == []


def test_a_loyalty_approval_is_bound_to_its_points(monkeypatch):
    """The finding: an approval for 100 points released any number of points."""
    dynamo = FakeDynamo(
        approval(action="adjust_loyalty", guestId="g1", points=100, adjustReason="x")
    )
    module, fake = wire_billing(monkeypatch, dynamo=dynamo, routes=PAID_ROUTES)
    result = module.adjust_loyalty(
        {"guestId": "g1", "points": 100000, "reason": "x", "approval_token": "t"}
    )
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == []


@pytest.mark.parametrize(
    "tool,changed",
    [
        ("post_charge", {"description": "something else"}),
        ("void_folio", {"reason": "a different reason"}),
        ("adjust_loyalty", {"reason": "a different reason"}),
    ],
)
def test_every_captured_argument_is_bound_not_just_the_money(monkeypatch, tool, changed):
    args = dict(TIER_2)[tool]
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action=tool, **MATCHING[tool])),
        routes=PAID_ROUTES,
    )
    result = getattr(module, tool)({**args, **changed, "approval_token": "t"})
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert not [c for c in fake.calls if c["method"] == "POST"]


@pytest.mark.parametrize(
    "extra",
    [{"chargeDate": "2020-01-01"}, {"chargeType": "ADJUSTMENT"}],
    ids=["back-dated", "different-charge-type"],
)
def test_an_argument_the_approval_did_not_capture_cannot_be_added(monkeypatch, extra):
    """No human decided the charge date or type, so the agent cannot either."""
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="post_charge", **MATCHING["post_charge"])),
        routes=PAID_ROUTES,
    )
    result = module.post_charge({**dict(TIER_2)["post_charge"], **extra, "approval_token": "t"})
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert not [c for c in fake.calls if c["method"] == "POST"]


def test_a_record_missing_a_field_it_should_carry_is_refused(monkeypatch):
    """Absent is not unbound: a record without its description approves nothing."""
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="post_charge", folioId="f1", amount=10)),
        routes=PAID_ROUTES,
    )
    result = module.post_charge({**dict(TIER_2)["post_charge"], "approval_token": "t"})
    assert result["error"]["code"] == "APPROVAL_INCOMPLETE"


def test_a_whitespace_only_token_is_treated_as_absent(monkeypatch):
    module, fake = wire_billing(monkeypatch, routes=PAID_ROUTES)
    result = module.post_charge(
        {"folioId": "f1", "description": "x", "amount": 1, "approval_token": "   "}
    )
    assert result["error"]["code"] == "APPROVAL_REQUIRED"


def test_billing_reads_need_no_approval_at_all(monkeypatch):
    """A3 is expected to investigate freely; only writes are gated."""
    module, fake = wire_billing(
        monkeypatch,
        routes={
            "GET /billing/folios": (200, ok({"folios": []})),
            "GET /billing/folios/f1": (200, ok({"folioId": "f1", "propertyId": "p1"})),
            "GET /loyalty/g1": (200, ok({})),
            "GET /loyalty/g1/transactions": (200, ok({})),
        },
    )
    assert module.list_folios({"propertyId": "p1"})["success"] is True
    assert module.get_folio({"propertyId": "p1", "folioId": "f1"})["success"] is True
    assert module.get_loyalty_profile({"guestId": "g1"})["success"] is True
    assert module.get_loyalty_transactions({"guestId": "g1"})["success"] is True
    assert len(fake.calls) == 4


# --- find_folio_by_reservation ---------------------------------------------- #
#
# The foundation has no folio-by-reservation lookup: GET /billing/folios filters on
# propertyId and status only. On A3's first real run -- woken by a checkout, which
# carries a reservation id -- it spent 24 sequential list_folios tool calls paging
# for the match before reading a single charge line. Every one was a model turn.
# These pin the loop into the Lambda, where it costs no turns.


class FolioPages(FakeFoundation):
    """``GET /billing/folios`` as real pages, plus the folio detail fetch."""

    def __init__(self, pages: list[list[dict]], detail: dict | None = None):
        super().__init__({})
        self.pages = pages
        self.detail = detail

    def call(self, method, surface, path, *, body=None, query=None, property_id=None):
        query = {k: v for k, v in (query or {}).items() if v is not None}
        self.calls.append(
            {"method": method, "surface": surface, "path": path, "body": body,
             "query": query, "property_id": property_id}
        )
        if path.startswith("/billing/folios/"):
            if self.detail is None:
                return {"status": 404, "ok": False,
                        "data": {"success": False, "error": {"code": "NOT_FOUND"}}}
            return {"status": 200, "ok": True, "data": ok(self.detail)}

        page = query.get("page", 1)
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return {
            "status": 200,
            "ok": True,
            "data": ok({
                "folios": rows,
                "pagination": {"page": page, "totalPages": len(self.pages)},
            }),
        }


def folio(folio_id: str, reservation_id: str) -> dict:
    return {"folioId": folio_id, "reservationId": reservation_id, "status": "OPEN"}


def wire_folio_scan(monkeypatch, pages, detail=None):
    module = load("billing")
    fake = FolioPages(pages, detail)
    monkeypatch.setattr(module, "client", fake)
    return module, fake


def test_the_folio_scan_walks_pages_and_returns_the_folio_in_full(monkeypatch):
    module, fake = wire_folio_scan(
        monkeypatch,
        [
            [folio("f1", "r1"), folio("f2", "r2")],
            [folio("f3", "r3"), folio("f4", "wanted")],
        ],
        detail={"folioId": "f4", "charges": [{"amount": 100}]},
    )
    result = module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "wanted"}
    )
    assert result["success"] is True
    data = result["data"]
    assert data["foundOnPage"] == 2
    assert data["scanned"] == 4
    # The full record, not the summary row: charge lines are the point.
    assert data["folio"]["charges"] == [{"amount": 100}]
    # One tool call, three API calls -- and crucially zero extra model turns.
    assert [c["path"] for c in fake.calls] == [
        "/billing/folios",
        "/billing/folios",
        "/billing/folios/f4",
    ]


def test_the_scan_stops_on_the_first_page_when_the_match_is_there(monkeypatch):
    module, fake = wire_folio_scan(
        monkeypatch, [[folio("f1", "r1")], [folio("f2", "r2")]], detail={"folioId": "f1"}
    )
    module.find_folio_by_reservation({"propertyId": "p1", "reservationId": "r1"})
    assert len([c for c in fake.calls if c["path"] == "/billing/folios"]) == 1


def test_a_reservation_with_no_folio_is_reported_as_such_not_as_an_error(monkeypatch):
    module, _ = wire_folio_scan(monkeypatch, [[folio("f1", "r1")]])
    data = module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "absent"}
    )["data"]
    assert data["folio"] is None
    assert data["scanned"] == 1
    assert "No folio at this property" in data["note"]


def test_an_incomplete_scan_is_never_reported_as_no_folio(monkeypatch):
    """The distinction A3 must not blur: "I checked everything and found nothing" and
    "I ran out of pages" would justify very different next steps."""
    module, fake = wire_folio_scan(
        monkeypatch, [[folio(f"f{n}", f"r{n}")] for n in range(40)]
    )
    result = module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "absent"}
    )
    assert result["success"] is False
    assert result["error"]["code"] == "SCAN_INCOMPLETE"
    assert "do not report it as one" in result["error"]["message"]
    assert len(fake.calls) == module.MAX_FOLIO_PAGES


def test_the_scan_returns_an_upstream_error_envelope_verbatim(monkeypatch):
    module, _ = wire(monkeypatch, "billing", {"GET /billing/folios": (403, {
        "success": False, "error": {"code": "FORBIDDEN", "message": "denied"}})})
    assert module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "r1"}
    ) == {"success": False, "error": {"code": "FORBIDDEN", "message": "denied"}}


def test_a_failed_detail_fetch_surfaces_rather_than_returning_the_summary(monkeypatch):
    """Returning the summary row would look like success and silently omit the
    charge lines the integrity check exists to read."""
    module, _ = wire_folio_scan(monkeypatch, [[folio("f1", "r1")]], detail=None)
    result = module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "r1"}
    )
    assert result["success"] is False
    assert result["error"]["code"] == "NOT_FOUND"


def test_the_folio_scan_needs_no_approval_because_it_is_a_read(monkeypatch):
    module, _ = wire_folio_scan(monkeypatch, [[folio("f1", "r1")]], detail={"folioId": "f1"})
    assert module.find_folio_by_reservation(
        {"propertyId": "p1", "reservationId": "r1"}
    )["success"] is True


def test_post_charge_defaults_to_service_and_omits_an_unset_charge_date(monkeypatch):
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(
            approval(folioId="f1", amount=45.0, description="Late checkout")
        ),
        routes=PAID_ROUTES,
    )
    module.post_charge(
        {
            "propertyId": "p1",
            "folioId": "f1",
            "description": "Late checkout",
            "amount": 45.0,
            "approval_token": "t",
        }
    )
    assert [c for c in fake.calls if c["method"] == "POST"][0]["body"] == {
        "chargeType": "SERVICE",
        "description": "Late checkout",
        "amount": 45.0,
    }


# --------------------------------------------------------------------------- #
# A4 night audit
# --------------------------------------------------------------------------- #


def daily(revenue=10000.0, rooms_sold=50, adr=200.0, occupied=48, total=100, percent=48.0):
    return ok(
        {
            "date": "2026-09-08",
            "revenue": revenue,
            "roomsSold": rooms_sold,
            "adr": adr,
            "occupancy": {"occupied": occupied, "totalRooms": total, "percent": percent},
        }
    )


def audit(revenue=10000.0, rooms_sold=50, adr=208.33, occupied=48, total=100, percent=48.0):
    return ok(
        {
            "metrics": {
                "dailyRevenue": revenue,
                "roomsSold": rooms_sold,
                "adr": adr,
                "occupiedRooms": occupied,
                "totalRooms": total,
                "occupancyPercent": percent,
            }
        }
    )


def test_compare_metrics_maps_the_two_endpoints_differing_field_names(monkeypatch):
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily()),
            "GET /audit/reports/p1": (200, audit()),
        },
    )
    data = module.compare_metrics({"propertyId": "p1"})["data"]
    assert data["comparison"]["roomsOccupied"] == {"auditReport": 48, "dailyReport": 48}
    assert data["comparison"]["roomRevenue"] == {"auditReport": 10000.0, "dailyReport": 10000.0}


def test_compare_metrics_explains_the_adr_divergence_rather_than_flagging_a_bug(monkeypatch):
    """Both endpoints publish 'adr' from different denominators: revenue over
    rooms occupied versus revenue over rooms sold. The disagreement is by design
    and A4 must say so."""
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily(adr=200.0, rooms_sold=50)),
            "GET /audit/reports/p1": (200, audit(adr=208.33, occupied=48)),
        },
    )
    data = module.compare_metrics({"propertyId": "p1"})["data"]
    adr = next(d for d in data["discrepancies"] if d["metric"] == "adr")
    assert adr["auditReport"] == 208.33 and adr["dailyReport"] == 200.0
    assert "occupied" in adr["cause"] and "sold" in adr["cause"]
    assert "Not a data error" in adr["cause"]


def test_compare_metrics_reports_a_genuine_discrepancy_without_a_cause_note(monkeypatch):
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily(occupied=48)),
            "GET /audit/reports/p1": (200, audit(occupied=41)),
        },
    )
    data = module.compare_metrics({"propertyId": "p1"})["data"]
    occ = next(d for d in data["discrepancies"] if d["metric"] == "roomsOccupied")
    assert (occ["auditReport"], occ["dailyReport"]) == (41, 48)
    assert "cause" not in occ, "only adr has a known benign explanation"


def test_agreeing_metrics_produce_an_empty_discrepancy_list(monkeypatch):
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily(adr=200.0)),
            "GET /audit/reports/p1": (200, audit(adr=200.0)),
        },
    )
    assert module.compare_metrics({"propertyId": "p1"})["data"]["discrepancies"] == []


def test_a_missing_audit_run_is_the_normal_pre_audit_state(monkeypatch):
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily()),
            "GET /audit/reports/p1": (404, {"success": False, "error": {"code": "NOT_FOUND"}}),
        },
    )
    data = module.compare_metrics({"propertyId": "p1"})["data"]
    assert data["auditReportAvailable"] is False
    assert data["auditReportStatus"] == 404
    assert "expected pre-audit state" in data["note"]
    # The live numbers are still returned, so A4 can report on the day regardless.
    assert data["dailyReport"]["revenue"] == 10000.0


def test_a_null_metric_on_one_side_is_not_reported_as_a_discrepancy(monkeypatch):
    partial = audit()
    partial["data"]["metrics"]["adr"] = None
    module, _ = wire(
        monkeypatch,
        "nightaudit",
        {
            "GET /reporting/p1/daily": (200, daily()),
            "GET /audit/reports/p1": (200, partial),
        },
    )
    data = module.compare_metrics({"propertyId": "p1"})["data"]
    assert [d["metric"] for d in data["discrepancies"]] == []
    # But it is still shown in the comparison, so the gap is visible.
    assert data["comparison"]["adr"]["auditReport"] is None


def test_nightaudit_exposes_no_write_tool_at_all():
    """A4 is Tier 3. POST /audit/runs is Admin-only and stays human-triggered."""
    module = load("nightaudit")
    assert set(module.router.tool_names) == {
        "daily_report", "audit_report", "compare_metrics",
        "list_stays", "list_folios", "room_board",
    }


# --------------------------------------------------------------------------- #
# A5 regional
# --------------------------------------------------------------------------- #


def test_range_metrics_defaults_to_a_trailing_30_day_window(monkeypatch):
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/range": (200, ok({}))})
    module.range_metrics({"propertyId": "p1"})
    query = fake.calls[0]["query"]
    from datetime import date, datetime

    start = datetime.strptime(query["startDate"], "%Y-%m-%d").date()
    end = datetime.strptime(query["endDate"], "%Y-%m-%d").date()
    assert end == date.today()
    assert (end - start).days == 29


def test_range_metrics_defaults_to_the_whole_chain(monkeypatch):
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/range": (200, ok({}))})
    module.range_metrics({})
    assert fake.calls[0]["query"]["propertyId"] == "_all"


def test_range_metrics_rejects_an_over_wide_span_before_calling(monkeypatch):
    """A client-side message the model can act on, instead of a bare 400."""
    module, fake = wire(monkeypatch, "regional", {})
    with pytest.raises(ValueError, match="caps a range at 92"):
        module.range_metrics({"startDate": "2026-01-01", "endDate": "2026-12-31"})
    assert fake.calls == []


def test_a_92_day_span_is_accepted_inclusively(monkeypatch):
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/range": (200, ok({}))})
    module.range_metrics({"startDate": "2026-01-01", "endDate": "2026-04-02"})  # 92 days
    assert fake.calls[0]["query"]["startDate"] == "2026-01-01"


def test_an_inverted_range_is_rejected(monkeypatch):
    module, _ = wire(monkeypatch, "regional", {})
    with pytest.raises(ValueError, match="is after endDate"):
        module.range_metrics({"startDate": "2026-06-01", "endDate": "2026-05-01"})


@pytest.mark.parametrize("value", ["08/09/2026", "yesterday", "2026-05-32", "2026-13-01"])
def test_a_malformed_date_names_the_field_that_was_wrong(monkeypatch, value):
    module, _ = wire(monkeypatch, "regional", {})
    with pytest.raises(ValueError, match="startDate must be YYYY-MM-DD"):
        module.range_metrics({"startDate": value, "endDate": "2026-05-01"})


def test_an_unpadded_date_is_accepted_because_strptime_accepts_it(monkeypatch):
    """``2026-4-2`` is not the documented format but parses unambiguously, and
    rejecting it would fail a request we can serve correctly."""
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/range": (200, ok({}))})
    module.range_metrics({"startDate": "2026-4-2", "endDate": "2026-05-01"})
    assert fake.calls[0]["query"]["startDate"] == "2026-04-02"


def test_an_empty_date_string_is_treated_as_unset_not_as_malformed(monkeypatch):
    """A model that means "no start date" may send "" rather than omitting the
    key; falling back to the default window is more useful than an error."""
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/range": (200, ok({}))})
    module.range_metrics({"startDate": "", "endDate": "2026-05-01"})
    assert fake.calls[0]["query"]["startDate"] == "2026-04-02"  # 30 days to 05-01


def test_occupancy_sends_no_property_id_because_the_endpoint_has_no_such_filter(monkeypatch):
    module, fake = wire(monkeypatch, "regional", {"GET /reporting/occupancy": (200, ok({}))})
    module.occupancy({"startDate": "2026-09-01", "endDate": "2026-09-08", "region": "West"})
    assert set(fake.calls[0]["query"]) == {"startDate", "endDate", "region"}


def test_regional_exposes_no_write_tool_at_all():
    """No write endpoint exists for rates, inventory, or availability."""
    module = load("regional")
    assert set(module.router.tool_names) == {
        "list_properties", "occupancy", "range_metrics", "daily_report",
    }


# --- the tool-side gate enforces the same bindings -------------------------- #
#
# Defence in depth. The Gateway interceptor is the authoritative gate and has its own
# tests; these exist because the interceptor is a separate deployable, and if it is
# ever detached or lags a newly added Tier-2 tool, this Lambda must still refuse.


def test_the_tool_gate_refuses_a_charge_on_a_folio_the_approval_did_not_name(monkeypatch):
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="post_charge", folioId="f-approved", amount=40)),
        routes=PAID_ROUTES,
    )
    result = module.post_charge(
        {
            "folioId": "f-other",
            "description": "x",
            "amount": 40,
            "approval_token": "t",
        }
    )
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == [], "refused before reaching the foundation"


def test_the_tool_gate_refuses_an_amount_the_approval_did_not_name(monkeypatch):
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="post_charge", folioId="f1", amount=40)),
        routes=PAID_ROUTES,
    )
    result = module.post_charge(
        {"folioId": "f1", "description": "x", "amount": 4000, "approval_token": "t"}
    )
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == []


def test_the_tool_gate_binds_a_loyalty_adjustment_to_its_guest(monkeypatch):
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="adjust_loyalty", guestId="g1")),
        routes=PAID_ROUTES,
    )
    assert module.adjust_loyalty(
        {"guestId": "g2", "points": 100, "reason": "x", "approval_token": "t"}
    )["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == []


def test_a_fully_bound_approval_still_lets_the_write_through(monkeypatch):
    """The gate must open, not merely refuse well."""
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(
            approval(
                action="post_charge", folioId="f1", amount=45.0, description="Late checkout"
            )
        ),
        routes=PAID_ROUTES,
    )
    result = module.post_charge(
        {
            "propertyId": "p1",
            "folioId": "f1",
            "description": "Late checkout",
            "amount": 45.0,
            "approval_token": "t",
        }
    )
    assert result["success"] is True
    assert len([c for c in fake.calls if c["method"] == "POST"]) == 1
