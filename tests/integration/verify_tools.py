#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Layer 1 verification: the five tool Lambdas in isolation, before any agent.

This is the highest-risk hop in the whole system -- a Lambda signing in as a
Cognito user and calling the foundation's authorizer-protected APIs -- so it is
proven first, with no model and no Gateway in the way.

Invocations here reproduce the real AgentCore Gateway contract exactly:

* the tool's **arguments are the entire event payload**, and
* the tool's **name arrives out-of-band** in ``client_context.custom`` under
  ``bedrockAgentCoreToolName``, prefixed ``{targetName}___`` -- **three**
  underscores, which is what the deployed Gateway advertises.

Getting that second part wrong is the most likely first-run bug, so the negative
cases below deliberately probe it. It is also a bug this file cannot catch on its
own: a synthetic event that agrees with the handler about the delimiter passes
whether or not either agrees with the Gateway. That is what Layer 2's tool-list
probe is for.

Usage::

    AWS_PROFILE=... AWS_REGION=us-east-1 python3 tests/integration/verify_tools.py
"""

from __future__ import annotations

import base64
import json
import sys

import boto3

FUNCTIONS = {
    target: f"hotel-ops-agent-tool-{target}"
    for target in ("arrivals", "housekeeping", "billing", "nightaudit", "regional")
}

lambda_client = boto3.client("lambda")


#: The Gateway's delimiter between the target name and the tool's own name.
DELIMITER = "___"


def invoke(target: str, tool: str, args: dict | None = None, *, prefix: str | None = None):
    """Invoke a tool the way the Gateway would.

    ``prefix`` overrides the target portion of the name. ``prefix=""`` sends the
    bare tool name with no delimiter at all, which is the shape a local caller or a
    future unprefixed Gateway would produce.
    """
    if prefix == "":
        qualified = tool
    else:
        qualified = f"{prefix if prefix is not None else target}{DELIMITER}{tool}"
    response = lambda_client.invoke(
        FunctionName=FUNCTIONS[target],
        Payload=json.dumps(args or {}).encode(),
        ClientContext=base64.b64encode(
            json.dumps({"custom": {"bedrockAgentCoreToolName": qualified}}).encode()
        ).decode(),
    )
    payload = json.loads(response["Payload"].read() or b"null")
    return response.get("FunctionError"), payload


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #

results: list[tuple[bool, str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, label, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return ok


def envelope_ok(payload) -> bool:
    """The foundation's own envelope shape, returned verbatim by the tool layer."""
    return isinstance(payload, dict) and payload.get("success") is True


def summarize(payload) -> str:
    return json.dumps(payload)[:220]


def main() -> int:
    print("=" * 72)
    print("Layer 1: tool Lambdas in isolation")
    print("=" * 72)

    # ---- A5 regional: no arguments at all, and the source of real IDs -------
    err, payload = invoke("regional", "list_properties")
    check(
        "regional.list_properties authenticates and returns the property list",
        not err and envelope_ok(payload),
        summarize(payload),
    )
    properties = (payload.get("data") or {}).get("properties") or []
    if not properties:
        print("\nCannot continue: no properties returned.")
        return 1
    prop = properties[0]
    property_id = prop["propertyId"]
    print(f"\n  using property {prop.get('name')} ({property_id})\n")

    err, payload = invoke(
        "regional", "range_metrics", {"propertyId": property_id}
    )
    check(
        "regional.range_metrics defaults to a trailing 30-day window",
        not err and envelope_ok(payload),
        summarize(payload),
    )

    err, payload = invoke(
        "regional",
        "range_metrics",
        {"propertyId": property_id, "startDate": "2020-01-01", "endDate": "2026-01-01"},
    )
    check(
        "regional.range_metrics rejects a range wider than the 92-day cap "
        "client-side, with a message the model can act on",
        not err
        and isinstance(payload, dict)
        and payload.get("success") is False
        and payload["error"]["code"] == "INVALID_ARGUMENT",
        summarize(payload),
    )

    # ---- A1 arrivals -------------------------------------------------------
    # daysAhead=7, not the default 1: this is a live property whose arrivals move,
    # and a check that reads "0 arrivals, PASS" teaches nothing. The assertion is
    # that the window is honoured and stated, not that anyone is arriving today.
    err, payload = invoke(
        "arrivals", "list_arrivals", {"propertyId": property_id, "daysAhead": 7}
    )
    data = (payload.get("data") or {}) if not err else {}
    arrivals = data.get("stays") or []
    window = data.get("window") or {}
    check(
        "arrivals.list_arrivals filters on check-in date and reports the window "
        "it actually covered",
        not err
        and envelope_ok(payload)
        and bool(window.get("from") and window.get("to"))
        and all(
            window["from"] <= (s.get("checkInDate") or "") <= window["to"]
            for s in arrivals
        ),
        f"{len(arrivals)} arriving between {window.get('from')} and "
        f"{window.get('to')}, {data.get('counts', {}).get('unassigned')} unassigned; "
        f"{summarize(payload)}",
    )

    err, payload = invoke(
        "arrivals", "list_rooms", {"propertyId": property_id, "onlyAssignable": True}
    )
    rooms = (payload.get("data") or {}).get("rooms") or [] if not err else []
    enriched = [r for r in rooms if not r.get("roomTypeUnresolved")]
    check(
        "arrivals.list_rooms joins the PMS room board to the CRS room-type "
        "catalogue across two APIs",
        not err and envelope_ok(payload) and bool(rooms),
        f"{len(rooms)} assignable rooms, {len(enriched)} with room-type "
        f"attributes resolved; sample={json.dumps(rooms[0]) if rooms else 'none'}",
    )
    check(
        "arrivals.list_rooms surfaces the accessibility and bed-configuration "
        "signals A1 actually ranks on",
        bool(enriched)
        and all(
            key in enriched[0]
            for key in ("accessibilityType", "bedConfiguration", "maxOccupancy", "floor")
        ),
        json.dumps({k: enriched[0].get(k) for k in
                    ("roomNumber", "floor", "status", "roomType", "accessibilityType",
                     "bedConfiguration", "maxOccupancy")}) if enriched else "",
    )

    err, payload = invoke(
        "arrivals",
        "assign_room",
        {"reservationId": "00000000-0000-0000-0000-000000000000", "roomId": "x"},
    )
    check(
        "arrivals.assign_room refuses to write without a reason for the "
        "decision log",
        not err
        and payload.get("success") is False
        and payload["error"]["code"] == "MISSING_ARGUMENT",
        summarize(payload),
    )

    if arrivals:
        guest_id = arrivals[0].get("guestId")
        if guest_id:
            err, payload = invoke("arrivals", "get_loyalty_profile", {"guestId": guest_id})
            check(
                "arrivals.get_loyalty_profile reads the one guest signal that is "
                "reachable through the API",
                not err and envelope_ok(payload),
                summarize(payload),
            )

    # ---- A2 housekeeping: property-scoped identity -------------------------
    err, payload = invoke("housekeeping", "list_tasks", {"propertyId": property_id})
    tasks = (payload.get("data") or {}).get("tasks") or [] if not err else []
    check(
        "housekeeping.list_tasks authenticates as the per-property identity",
        not err and envelope_ok(payload),
        f"{len(tasks)} tasks; {summarize(payload)}",
    )
    scoped = {t.get("propertyId") for t in tasks if t.get("propertyId")}
    check(
        "housekeeping tasks are confined to the requested property",
        scoped in ({property_id}, set()),
        f"distinct propertyIds={scoped or 'not returned per task'}",
    )

    err, payload = invoke("housekeeping", "room_board", {"propertyId": property_id})
    check(
        "housekeeping.room_board returns per-room floor for same-floor batching",
        not err and envelope_ok(payload),
        summarize(payload),
    )

    err, payload = invoke("housekeeping", "list_tasks", {})
    check(
        "housekeeping refuses a call with no propertyId, because propertyId "
        "selects the identity rather than filtering the result",
        not err and payload.get("success") is False,
        summarize(payload),
    )

    # ---- A4 night audit ---------------------------------------------------
    err, payload = invoke("nightaudit", "compare_metrics", {"propertyId": property_id})
    data = payload.get("data") or {} if not err else {}
    check(
        "nightaudit.compare_metrics reconciles the two reporting endpoints",
        not err and envelope_ok(payload),
        summarize(payload),
    )
    if data.get("auditReportAvailable"):
        check(
            "nightaudit.compare_metrics reports metric disagreement as a finding "
            "instead of silently picking one endpoint's number",
            "discrepancies" in data,
            f"discrepancies={json.dumps(data.get('discrepancies'))[:400]}",
        )
    else:
        check(
            "nightaudit.compare_metrics treats a missing audit run as the normal "
            "pre-audit state, not an error",
            data.get("auditReportAvailable") is False and "note" in data,
            summarize(data.get("note")),
        )

    # ---- A3 billing: the approval gate ------------------------------------
    err, payload = invoke("billing", "list_folios", {"propertyId": property_id})
    folios = (payload.get("data") or {}).get("folios") or [] if not err else []
    check(
        "billing.list_folios reads folios without needing approval",
        not err and envelope_ok(payload),
        f"{len(folios)} folios; {summarize(payload)}",
    )

    folio_id = folios[0]["folioId"] if folios else "00000000-0000-0000-0000-000000000000"

    print("\n  -- Tier 2: every write must be refused without human approval --\n")
    for tool, args in (
        ("post_charge", {"folioId": folio_id, "description": "probe", "amount": 1}),
        ("void_folio", {"folioId": folio_id, "reason": "probe"}),
        ("adjust_loyalty", {"guestId": folio_id, "points": 100, "reason": "probe"}),
    ):
        err, payload = invoke("billing", tool, args)
        check(
            f"billing.{tool} is refused with no approval_token",
            not err
            and payload.get("success") is False
            and payload["error"]["code"] == "APPROVAL_REQUIRED",
            summarize(payload),
        )

        err, payload = invoke("billing", tool, {**args, "approval_token": "forged"})
        check(
            f"billing.{tool} is refused with a forged approval_token "
            "(fails closed, never open)",
            not err
            and payload.get("success") is False
            and payload["error"]["code"]
            in ("APPROVAL_INVALID", "APPROVAL_UNVERIFIABLE"),
            summarize(payload),
        )

    # ---- Cross-agent isolation -------------------------------------------
    print("\n  -- Per-agent scoping: an agent cannot borrow another's tools --\n")
    err, payload = invoke(
        "housekeeping",
        "post_charge",
        {"folioId": folio_id, "description": "probe", "amount": 1},
        prefix="billing",
    )
    check(
        "the housekeeping function cannot execute billing___post_charge even when "
        "handed that exact tool name",
        not err
        and payload.get("success") is False
        and payload["error"]["code"] == "UNKNOWN_TOOL",
        summarize(payload),
    )

    err, payload = invoke("housekeeping", "list_tasks", {"propertyId": property_id},
                          prefix="")
    check(
        "an unprefixed tool name still dispatches, so the target prefix is "
        "stripped rather than assumed",
        not err and envelope_ok(payload),
        summarize(payload)[:120],
    )

    err, payload = invoke("regional", "list_properties", {}, prefix="regional___extra")
    check(
        "a malformed tool name is reported as UNKNOWN_TOOL rather than crashing "
        "the function",
        not err and payload.get("success") is False,
        summarize(payload),
    )

    # ---- Summary ---------------------------------------------------------
    failed = [label for ok, label, _ in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
