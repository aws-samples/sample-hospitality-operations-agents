# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway target ``billing`` -- A3, Billing & Folio Integrity (Tier 2).

Signs in as ``agent-billing@…`` (Cognito group **Manager**, chain-level, so
``propertyId`` is passed explicitly).

**Every write on this target is Tier 2: propose-and-confirm.** The rule from
``hotel-operations-agent.md`` §7 is that an agent may reorganize work freely and
must never move money on its own. Three tools move money -- ``post_charge``,
``void_folio``, ``adjust_loyalty`` -- and each is rejected without a valid
``approval_token`` minted by a human in the ops console.

Where the gate lives, and why there are two of them
---------------------------------------------------
The authoritative gate is the Gateway **request interceptor**, outside this
Lambda and outside the model's reach: it inspects the tool name and rejects a
Tier-2 call with no token before this code ever runs. That is what makes the
guardrail unbypassable by prompt injection or a confused model.

The check below is a second, independent gate. It exists because the interceptor
is a separate deployable: if it is ever misconfigured, detached, or deployed at a
version that does not know about a newly added Tier-2 tool, this Lambda still
refuses to move money. Defence in depth on the one rule that matters most.
"""

from __future__ import annotations

import os

from hotel_ops.foundation_client import FoundationClient
from hotel_ops.tool_dispatch import ToolRouter, tool_error

router = ToolRouter("billing")
client = FoundationClient()

DEFAULT_LIMIT = int(os.environ.get("DEFAULT_PAGE_LIMIT", "50"))

#: Where the ops console records approvals it has granted. Written by the API
#: stack, read here. A token is TTL'd, and bound to one action, target and every
#: argument the proposal captured.
APPROVALS_TABLE = os.environ.get("APPROVALS_TABLE", "")

_dynamodb = None


def _approvals():
    global _dynamodb
    if _dynamodb is None:
        import boto3

        _dynamodb = boto3.client("dynamodb")
    return _dynamodb


def require_approval(args: dict, *, action: str) -> dict | None:
    """Return an error envelope if this write is not humanly approved.

    Returns ``None`` when the call may proceed. The token must exist, be APPROVED,
    be unexpired, and be bound to *this* action against *this* target for *this*
    amount -- an approval to void one folio is not an approval to charge another.

    It is **not** single-use, and the earlier version of this docstring claiming
    otherwise was wrong: nothing here or in the Gateway interceptor consumes the
    token, deliberately, because the Gateway may retry an interceptor invocation and
    a gate that burned the token on a retry would refuse a write the human really
    did approve. Containment comes from the bindings above plus a fifteen-minute TTL,
    not from consumption.
    """
    token = (args.get("approval_token") or "").strip()
    if not token:
        return tool_error(
            "APPROVAL_REQUIRED",
            f"{action!r} moves money and is Tier 2 (propose-and-confirm). File a "
            "proposal for a human to approve in the ops console, then retry with "
            "the approval_token they issue. Do not attempt to bypass this.",
            action=action,
            tier=2,
        )

    if not APPROVALS_TABLE:
        # Fail closed. An unconfigured approvals table must never read as
        # "approved"; that would invert the guardrail.
        return tool_error(
            "APPROVAL_UNVERIFIABLE",
            "APPROVALS_TABLE is not configured, so this approval token cannot be "
            "verified. Refusing the write.",
            action=action,
        )

    try:
        record = _approvals().get_item(
            TableName=APPROVALS_TABLE,
            Key={"approvalId": {"S": token}},
            ConsistentRead=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, and say why
        return tool_error(
            "APPROVAL_UNVERIFIABLE",
            f"Could not verify the approval token: {type(exc).__name__}: {exc}. "
            "Refusing the write.",
            action=action,
        )

    item = record.get("Item")
    if not item:
        return tool_error(
            "APPROVAL_INVALID",
            "That approval_token does not exist or has expired.",
            action=action,
        )
    status = item.get("status", {}).get("S")
    if status != "APPROVED":
        return tool_error(
            "APPROVAL_INVALID",
            f"That approval is in state {status!r}, not APPROVED.",
            action=action,
        )
    # A record with no action is refused: the console always records one.
    granted_for = item.get("action", {}).get("S")
    if granted_for != action:
        return tool_error(
            "APPROVAL_MISMATCH",
            f"That approval was granted for {granted_for!r}, not {action!r}.",
            action=action,
            grantedFor=granted_for,
        )

    # And to the property it was filed against. A folio's property is checked
    # against ``propertyId`` separately (see :func:`_folio_in_scope`); this closes the
    # remaining gap, a propertyId that names a different hotel from the approval's.
    approved_property = item.get("propertyId", {}).get("S")
    presented_property = args.get("propertyId")
    if approved_property and presented_property and presented_property != approved_property:
        return tool_error(
            "APPROVAL_MISMATCH",
            f"That approval was filed for property {approved_property!r}, not "
            f"{presented_property!r}.",
            action=action,
            boundTo=approved_property,
        )

    # Every argument the proposal captured must be recorded and match, and none it
    # did not capture may be added. Mirrors the Gateway interceptor's
    # _binding_mismatch; both exist so neither being misconfigured opens the gate.
    # ``points`` was once bound by neither, so a 100-point approval released any
    # number of points.
    bound = BOUND_ARGUMENTS[action]
    for field, argument in bound.items():
        attribute = item.get(field) or {}
        approved = attribute.get("S", attribute.get("N"))
        if approved is None:
            return tool_error(
                "APPROVAL_INCOMPLETE",
                f"That approval does not record {field}; file a fresh proposal.",
                action=action,
            )
        if not _same(field, approved, args.get(argument)):
            return tool_error(
                "APPROVAL_MISMATCH",
                f"That approval is bound to {argument}={approved!r}, not "
                f"{args.get(argument)!r}.",
                action=action,
                boundTo=approved,
            )
    for argument, value in args.items():
        if argument in ALWAYS_ALLOWED or argument in bound.values():
            continue
        if argument in UNCAPTURED_DEFAULTS and value == UNCAPTURED_DEFAULTS[argument]:
            continue
        return tool_error(
            "APPROVAL_MISMATCH",
            f"{argument!r} was not part of the approval, so it cannot be set here.",
            action=action,
        )

    return None


#: Approval field -> tool argument it must equal. Must agree with the Gateway
#: interceptor's BOUND_ARGUMENTS and the console's GATED_ACTIONS.
BOUND_ARGUMENTS: dict[str, dict[str, str]] = {
    "post_charge": {"folioId": "folioId", "amount": "amount", "description": "description"},
    "void_folio": {"folioId": "folioId", "voidReason": "reason"},
    "adjust_loyalty": {"guestId": "guestId", "points": "points", "adjustReason": "reason"},
}
NUMERIC_FIELDS = frozenset({"amount", "points"})
ALWAYS_ALLOWED = frozenset({"propertyId", "approval_token"})
UNCAPTURED_DEFAULTS = {"chargeType": "SERVICE"}


def _same(field: str, approved: str, presented: object) -> bool:
    if field in NUMERIC_FIELDS:
        from decimal import Decimal, InvalidOperation

        try:
            return Decimal(str(presented)) == Decimal(approved)
        except (InvalidOperation, ValueError, TypeError):
            return False
    return isinstance(presented, str) and presented == approved


def _folio_in_scope(args: dict) -> tuple[dict | None, dict | None]:
    """Read a folio and confirm it belongs to ``args["propertyId"]``.

    Returns ``(envelope, None)`` when it does and ``(None, refusal)`` when it does
    not, or when the read itself failed (the platform's envelope, verbatim).

    This check exists because the billing identity is chain-level. The platform's
    own ``verify_property_access`` passes a chain-level Manager for *every* folio,
    so on its own it scopes nothing: a folio id from another hotel -- pasted into a
    chat, or named in guest data the model read -- would come back in full, and a
    released approval could move money on it. The Gateway's request interceptor pins
    ``propertyId`` to the run's property; this is the other half, which proves the
    folio is actually there.
    """
    result = client.get("pms", f"/billing/folios/{args['folioId']}")
    if not result["ok"]:
        return None, result["data"]
    folio = ((result["data"] or {}).get("data")) or {}
    actual = folio.get("propertyId")
    if actual != args["propertyId"]:
        return None, tool_error(
            "OUT_OF_SCOPE",
            f"Folio {args['folioId']} is not at property {args['propertyId']}. Only "
            "folios at this run's property can be read or changed.",
            folioId=args["folioId"],
        )
    return result["data"], None


# --------------------------------------------------------------------------- #
# Reads -- no approval needed. A3 is expected to investigate freely.
# --------------------------------------------------------------------------- #


@router.tool("list_folios")
def list_folios(args: dict) -> dict:
    """Folios at one property, optionally filtered by status."""
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


@router.tool("get_folio")
def get_folio(args: dict) -> dict:
    """One folio in full, with its charge and payment lines.

    This is the read that has to happen before any proposal: duplicate and
    missing charges are only visible at line level.

    ``propertyId`` is required, and a folio at a different property is refused
    rather than returned. See :func:`_folio_in_scope`.
    """
    folio, refusal = _folio_in_scope(args)
    return refusal if refusal else folio


#: Pages walked looking for one reservation's folio. At 100 rows a page this covers
#: a property with 1,500 folios; beyond that the reply says it gave up rather than
#: reporting "no folio", because those are very different facts.
MAX_FOLIO_PAGES = 20
FOLIO_SCAN_LIMIT = 100


@router.tool("find_folio_by_reservation")
def find_folio_by_reservation(args: dict) -> dict:
    """The folio for one reservation, found by scanning -- and why that is needed.

    ``GET /billing/folios`` filters on ``propertyId`` and ``status`` and nothing
    else; there is no folio-by-reservation lookup anywhere in the foundation, and
    ``GET /billing/folios/{id}`` needs the folio id you are trying to find. So the
    only way to resolve a reservation to its folio is to page the list.

    That paging belongs here rather than in the model. A3's reactive trigger
    arrives carrying a reservation id -- a checkout just happened -- and on its
    first real run A3 spent **24 sequential ``list_folios`` tool calls** walking
    pages itself before it could look at a single charge. Each one was a model turn:
    tokens, latency, and 24 chances to lose track. Moving the loop into the Lambda
    makes it one tool call, and the foundation sees fewer, larger, paced requests.

    Returns the matching folio in full, so the caller does not then have to call
    ``get_folio`` -- finding it and reading it is one intention, and splitting them
    would put the round trip straight back.
    """
    property_id = args["propertyId"]
    reservation_id = args["reservationId"]

    scanned = 0
    for page in range(1, MAX_FOLIO_PAGES + 1):
        result = client.get(
            "pms",
            "/billing/folios",
            query={
                "propertyId": property_id,
                "page": page,
                "limit": FOLIO_SCAN_LIMIT,
            },
        )
        if not result["ok"]:
            return result["data"]

        payload = (result["data"] or {}).get("data") or {}
        folios = payload.get("folios") or []
        scanned += len(folios)

        for folio in folios:
            if folio.get("reservationId") == reservation_id:
                # Fetch the full record: the summary row carries no charge lines,
                # and charge lines are the entire point of an integrity check.
                detail = client.get("pms", f"/billing/folios/{folio['folioId']}")
                if not detail["ok"]:
                    return detail["data"]
                full = (detail["data"] or {}).get("data") or {}
                return {
                    "success": True,
                    "data": {
                        "reservationId": reservation_id,
                        "propertyId": property_id,
                        "folio": full or folio,
                        "foundOnPage": page,
                        "scanned": scanned,
                    },
                }

        pagination = payload.get("pagination") or {}
        if not folios or page >= (pagination.get("totalPages") or page):
            return {
                "success": True,
                "data": {
                    "reservationId": reservation_id,
                    "propertyId": property_id,
                    "folio": None,
                    "scanned": scanned,
                    "note": (
                        f"No folio at this property references reservation "
                        f"{reservation_id}, after checking all {scanned} of them. "
                        "Either the reservation belongs to another property or no "
                        "folio was ever opened for it."
                    ),
                },
            }

    return tool_error(
        "SCAN_INCOMPLETE",
        f"Checked {scanned} folios across {MAX_FOLIO_PAGES} pages without finding "
        f"reservation {reservation_id}, and there are more. This is not 'no folio "
        "exists' -- do not report it as one. Ask a human to look it up directly.",
        reservationId=reservation_id,
        scanned=scanned,
    )


@router.tool("get_loyalty_profile")
def get_loyalty_profile(args: dict) -> dict:
    """Loyalty standing for one guest, to size a goodwill adjustment sanely."""
    return client.get("pms", f"/loyalty/{args['guestId']}")["data"]


@router.tool("get_loyalty_transactions")
def get_loyalty_transactions(args: dict) -> dict:
    """Loyalty ledger for one guest -- shows whether an adjustment already landed."""
    return client.get(
        "pms",
        f"/loyalty/{args['guestId']}/transactions",
        query={"page": args.get("page"), "limit": args.get("limit", DEFAULT_LIMIT)},
    )["data"]


# --------------------------------------------------------------------------- #
# Writes -- Tier 2. Every one gated.
# --------------------------------------------------------------------------- #


@router.tool("post_charge")
def post_charge(args: dict) -> dict:
    """Post a charge or a negative adjustment to a folio. **Requires approval.**

    ``chargeType`` is ``SERVICE`` for a real charge or ``ADJUSTMENT`` for a
    correction; a correction that removes money is an ``ADJUSTMENT`` with a
    negative ``amount``. ``amount`` must be non-zero.
    """
    denial = require_approval(args, action="post_charge")
    if denial:
        return denial
    _, refusal = _folio_in_scope(args)
    if refusal:
        return refusal

    body = {
        "chargeType": args.get("chargeType", "SERVICE"),
        "description": args["description"],
        "amount": args["amount"],
    }
    if args.get("chargeDate"):
        body["chargeDate"] = args["chargeDate"]

    return client.post(
        "pms", f"/billing/folios/{args['folioId']}/charges", body=body
    )["data"]


@router.tool("void_folio")
def void_folio(args: dict) -> dict:
    """Void a folio. **Requires approval.**

    Note the scope carefully: the foundation exposes
    ``POST /billing/folios/{folioId}/void``, which voids the **whole folio**.
    There is no endpoint to void a single charge line. To reverse one line, post
    a negative ``ADJUSTMENT`` with ``post_charge`` instead.
    """
    denial = require_approval(args, action="void_folio")
    if denial:
        return denial
    _, refusal = _folio_in_scope(args)
    if refusal:
        return refusal

    return client.post(
        "pms",
        f"/billing/folios/{args['folioId']}/void",
        body={"reason": args["reason"]},
    )["data"]


@router.tool("adjust_loyalty")
def adjust_loyalty(args: dict) -> dict:
    """Adjust a guest's loyalty points. **Requires approval.**

    ``reason`` is mandatory at the foundation too (max 500 chars) and lands in the
    guest's loyalty ledger, so it is guest-visible in effect. ``points`` may be
    negative but not zero.
    """
    denial = require_approval(args, action="adjust_loyalty")
    if denial:
        return denial

    return client.post(
        "pms",
        f"/loyalty/{args['guestId']}/adjust",
        body={"points": args["points"], "reason": args["reason"]},
    )["data"]


def handler(event, context):
    return router.dispatch(event, context)
