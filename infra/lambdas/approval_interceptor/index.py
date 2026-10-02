# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway REQUEST interceptor: run scope, caller authority, and the Tier-2 gate.

Three checks, in this order, on every tool call before the target Lambda runs:

1. **Property scope.** A run about one hotel may only touch that hotel. The run's
   property arrives as the ``X-Hotel-Ops-Property-Id`` header, set by the Runtime
   from out-of-band context the model cannot reach (``agents/gateway.py`` explains
   why an unsigned header is trustworthy here), and a call whose ``propertyId``
   names any other property is refused. So is a call with no ``propertyId`` at all,
   unless the tool is known to be property-neutral. Before this existed the run's
   property reached the tools only as prompt text, and the tool identities are
   chain-level, so a front-desk user at one hotel could have had the copilot read
   another hotel's folios just by naming one.
2. **Caller authority.** In a run a human asked for, a tool is allowed only if the
   platform itself would let one of that human's Cognito groups call the endpoint
   behind it (:data:`TOOL_GROUPS`, transcribed from the platform's
   ``require_groups``). The agent identities are Managers, so without this a
   front-desk user's copilot could pre-assign rooms -- which the platform reserves
   for Managers.
3. **The Tier-2 approval gate.** ``hotel-operations-agent.md`` §7 draws one line the
   system must never cross -- an agent may reorganize work freely and must never
   move money on its own. Three tools move money: ``billing___post_charge``,
   ``billing___void_folio``, ``billing___adjust_loyalty``. All three are refused
   unless the run carries an approval a human released in the ops console for
   *that* action, target, amount and property.

The approval never passes through the model
-------------------------------------------
It arrives as the ``X-Hotel-Ops-Approval-Token`` header, present only on the
execution run a release starts. Any ``approval_token`` the model writes into the
arguments is discarded, and the header's value is put there instead, for the billing
Lambda's own independent check. It used to travel in the execution prompt, which
meant AgentCore Memory stored it, traces recorded it, and on the first real approval
the model repeated it in its answer.

The delimiter is not assumed
----------------------------
The Gateway prefixes a target's tool names with the target name and a run of
underscores -- **three** on the deployed Gateway, which advertises
``billing___post_charge``. An earlier version of this file gated on a hard-coded
two-underscore spelling, so no live tool name ever matched the gated set and every
Tier-2 write would have passed straight through. A guardrail that fails open
because of a delimiter width is worse than no guardrail, because it reads as
present. So the name is now *parsed* into ``(target, action)`` on any run of two or
more underscores and gated on the pair.

Why the gate lives here
-----------------------
This code runs inside the Gateway, before the target Lambda is invoked, before
any Cognito token is minted, and entirely outside the model's reach. A prompt
injection can talk a model into *trying* to post a charge; it cannot talk this
function into approving one, because the model has no channel to it other than
the tool call itself. ``tools/billing/handler.py`` repeats the check as defence
in depth -- if this interceptor is ever detached or deployed at a version that
does not know about a newly added Tier-2 tool, the target still refuses.

Refusals are returned as an MCP *tool* error
--------------------------------------------
A short-circuit ``transformedGatewayResponse`` carrying a JSON-RPC ``error``
object reads to an MCP client as a transport failure, and Strands surfaces that
as an exception that ends the turn. A ``result`` with ``isError: true`` instead
reaches the model as a tool result it can read, understand, and act on -- which
is the whole point: the correct next move is "file a proposal for a human", and
the model can only make it if it is told.

Idempotency
-----------
The Gateway may retry an interceptor invocation (AWS documents this explicitly),
so nothing here mutates state. The read is a ``GetItem``; consuming or expiring
an approval is the ops console's job, and an interceptor that burned a token on a
retried invocation would refuse a write the human did approve.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterator

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: Written by the ops console API, read here. Set by ``agentcore_stack``.
APPROVALS_TABLE = os.environ.get("APPROVALS_TABLE", "")

#: Splits a Gateway tool name into its target and the tool's own name, on any run
#: of two or more underscores. Anchored and greedy on the underscores so
#: ``billing___post_charge`` yields ``("billing", "post_charge")`` and not
#: ``("billing", "_post_charge")``.
TOOL_NAME = re.compile(r"^(?P<target>[A-Za-z0-9]+)_{2,}(?P<action>.+)$")

#: ``(target, action)`` pairs that require a human approval. Qualified by target on
#: purpose: ``("regional", "post_charge")`` does not exist, but if some future
#: target grew a tool called ``post_charge`` we would want to make that decision
#: deliberately rather than inherit a gate by name collision.
#:
#: The action recorded on the approval record is the *unprefixed* tool name, so
#: an approval to void a folio can never authorize a charge.
GATED_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("billing", "post_charge"),
        ("billing", "void_folio"),
        ("billing", "adjust_loyalty"),
    }
)

#: Headers set by ``agents/gateway.py``. Lower-cased on lookup.
PROPERTY_HEADER = "x-hotel-ops-property-id"
GROUPS_HEADER = "x-hotel-ops-caller-groups"
TOKEN_HEADER = "x-hotel-ops-approval-token"  # nosec B105 - a header name, not a token
TRIGGER_HEADER = "x-hotel-ops-trigger"

#: What the property header carries for a chain-wide run. Only a chain-level caller
#: (or a schedule, which has no caller) produces one: the console refuses a
#: chain-wide question from anyone bound to a property.
CHAIN_WIDE = "_chain"

#: Tools that take no ``propertyId`` and are still safe in a property-scoped run,
#: because what they touch is not owned by a property. Guests and their loyalty
#: accounts are chain-level records: the platform's loyalty endpoints apply no
#: property check even to a property-scoped caller. ``adjust_loyalty`` is here for the
#: same reason, and is still behind the approval gate.
PROPERTY_NEUTRAL: frozenset[tuple[str, str]] = frozenset(
    {
        ("arrivals", "get_loyalty_profile"),
        ("billing", "get_loyalty_profile"),
        ("billing", "get_loyalty_transactions"),
        ("billing", "adjust_loyalty"),
    }
)

# The platform's own group rules, per tool, from each endpoint handler's
# ``require_groups``. Where a tool calls two endpoints, its set is the intersection.
_FRONT = frozenset({"FrontDesk", "Manager", "Admin"})
_HOUSEKEEPING = frozenset({"Housekeeping", "Manager", "Admin"})
_MANAGERS = frozenset({"Manager", "Admin"})
_REPORTING = frozenset({"Manager", "Admin", "RegionalManager", "RevenueManager"})
_ROOM_BOARD = frozenset({"Housekeeping", "FrontDesk", "Manager", "Admin", "RevenueManager"})
_EVERYONE = frozenset(
    {"FrontDesk", "Housekeeping", "Manager", "Admin", "RevenueManager", "RegionalManager"}
)

#: Which of a human operator's groups may use each tool in a run they asked for.
#: A tool absent from this map is refused in such a run -- fail closed, so a tool
#: added later is not silently open to everyone.
TOOL_GROUPS: dict[tuple[str, str], frozenset[str]] = {
    ("arrivals", "list_arrivals"): _FRONT,  # GET /stays
    ("arrivals", "get_loyalty_profile"): _FRONT,  # GET /loyalty/{guestId}
    ("arrivals", "list_rooms"): _ROOM_BOARD,  # rooms summary + room types (open)
    ("arrivals", "assign_room"): _MANAGERS,  # PUT /stays/{id}/assign-room
    ("arrivals", "check_in"): _FRONT,  # POST /stays/{id}/checkin
    ("billing", "list_folios"): _FRONT,
    ("billing", "get_folio"): _FRONT,
    ("billing", "find_folio_by_reservation"): _FRONT,
    ("billing", "get_loyalty_profile"): _FRONT,
    ("billing", "get_loyalty_transactions"): _FRONT,
    ("billing", "post_charge"): _MANAGERS,
    ("billing", "void_folio"): _MANAGERS,
    ("billing", "adjust_loyalty"): _MANAGERS,
    ("housekeeping", "list_tasks"): _HOUSEKEEPING,
    ("housekeeping", "get_task"): _HOUSEKEEPING,
    ("housekeeping", "room_board"): _ROOM_BOARD,
    ("housekeeping", "assign_task"): _HOUSEKEEPING,
    ("housekeeping", "complete_task"): _HOUSEKEEPING,
    ("housekeeping", "inspect_task"): _HOUSEKEEPING,
    ("nightaudit", "daily_report"): _REPORTING,
    ("nightaudit", "audit_report"): _MANAGERS,
    ("nightaudit", "compare_metrics"): _MANAGERS,  # daily report + audit reports
    ("nightaudit", "list_stays"): _FRONT,
    ("nightaudit", "list_folios"): _FRONT,
    ("nightaudit", "room_board"): _ROOM_BOARD,
    ("regional", "list_properties"): _EVERYONE,
    ("regional", "occupancy"): _REPORTING,
    ("regional", "range_metrics"): _REPORTING,
    ("regional", "daily_report"): _REPORTING,
}

#: TTL attribute on ``hotel-ops-agent-approvals``. DynamoDB deletes expired items
#: lazily -- up to 48 hours late -- so an item that is still present may already
#: be expired. Checked explicitly rather than trusting the sweeper.
TTL_ATTRIBUTE = "expiresAt"

OUTPUT_VERSION = "1.0"

_dynamodb = None


def _approvals():
    """Lazy client, so a cold start that only sees read tools pays nothing."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.client("dynamodb")
    return _dynamodb


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


def handler(event: dict, context: Any) -> dict:
    """Inspect one Gateway request; pass it through or refuse it."""
    request = ((event or {}).get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body")
    headers = {str(k).lower(): v for k, v in (request.get("headers") or {}).items()}
    scope = RunScope.from_headers(headers)

    for call_id, tool_name, arguments in _tool_calls(body):
        target, action = _split_tool_name(tool_name)
        gated = (target, action) in GATED_TOOLS
        if gated:
            # Whatever the model wrote here is not an approval. Only the header is.
            arguments.pop("approval_token", None)

        denial = _scope_denial(target, action, arguments, scope) or (
            _denial_reason(action, arguments, scope) if gated else None
        )
        if denial is None:
            if gated:
                # For tools/billing/handler.py's own, independent check.
                arguments["approval_token"] = scope.approval_token
                logger.info("approval ok tool=%s", tool_name)
            continue
        # Refuse the whole request. A batch cannot be partially short-circuited
        # -- returning a response at all skips the target entirely -- so one
        # refused call in a batch refuses every call in it.
        code, reason = denial
        logger.warning("refused tool=%s code=%s reason=%s", tool_name, code, reason)
        return _refusal(
            call_id, tool_name, code, reason, batched=isinstance(body, list)
        )

    # Pass through -- unchanged, except that a gated call now carries the released
    # approval in place of anything the model wrote. Echoes the *parsed* body, not
    # rawGatewayRequest: the Gateway re-serializes it, and handing back a raw
    # string here would be a type error rather than a passthrough.
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def _tool_calls(body: object) -> Iterator[tuple[object, str, dict]]:
    """Yield ``(id, toolName, arguments)`` for every tool call in the request.

    Yields nothing for ``initialize``, ``tools/list``, ``ping``, notifications,
    and anything unparseable. An unrecognized shape is passed through rather than
    refused: the Gateway validates JSON-RPC itself, and a body this function
    cannot read is a body in which it cannot have found a gated tool. The gate
    fails closed on the thing it *can* see -- a gated call with no valid token.
    """
    # JSON-RPC batching: dropped from the MCP spec in the 2025-06-18 revision and
    # not something AgentCore's client emits today, but a single unapproved call
    # hidden in a batch is exactly the shape a bypass attempt would take.
    messages = body if isinstance(body, list) else [body]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        name = params.get("name")
        if not isinstance(name, str):
            continue
        # The dict yielded is the one in the body, so the handler's edits to it --
        # discarding a model-written token, attaching the real one -- are what the
        # target receives.
        if not isinstance(params.get("arguments"), dict):
            params["arguments"] = {}
        yield message.get("id"), name, params["arguments"]


def _split_tool_name(tool_name: str) -> tuple[str, str]:
    """``"billing___post_charge"`` -> ``("billing", "post_charge")``.

    An unprefixed name yields ``("", name)``, which matches nothing in
    :data:`GATED_TOOLS` -- correct, because a name with no target is not a name the
    Gateway produced.
    """
    match = TOOL_NAME.match(tool_name)
    return (match["target"], match["action"]) if match else ("", tool_name)


class RunScope:
    """What the Runtime says this run is, read from the headers it sent."""

    def __init__(self, property_id: str | None, groups: frozenset[str] | None, token: str | None):
        #: ``None`` for a chain-wide run, or when no header arrived at all.
        self.property_id = property_id
        #: ``None`` when no human asked -- a schedule or an event.
        self.caller_groups = groups
        self.approval_token = token

    @classmethod
    def from_headers(cls, headers: dict) -> "RunScope":
        raw_property = (headers.get(PROPERTY_HEADER) or "").strip()
        if not raw_property:
            # Every run the Runtime makes sends this header. Its absence means a
            # caller outside the agent graph -- someone holding InvokeGateway,
            # which is an operator, not the model. Logged, not refused: refusing
            # would break the no-model verification scripts for no gain.
            logger.warning("no %s header; property scope not applied", PROPERTY_HEADER)
        property_id = None if raw_property in ("", CHAIN_WIDE) else raw_property

        raw_groups = headers.get(GROUPS_HEADER)
        groups = (
            frozenset(g.strip() for g in raw_groups.split(",") if g.strip())
            if isinstance(raw_groups, str)
            else None
        )
        token = (headers.get(TOKEN_HEADER) or "").strip() or None
        # A chat run always carries the groups header, empty or not. One that
        # arrives without it is malformed, and is treated as a human with no
        # authority rather than as an unattended run with the agents' full one.
        if groups is None and (headers.get(TRIGGER_HEADER) or "").strip() == "chat":
            logger.warning("chat run without %s; failing closed", GROUPS_HEADER)
            groups = frozenset()
        return cls(property_id, groups, token)


def _scope_denial(
    target: str, action: str, arguments: dict, scope: RunScope
) -> tuple[str, str] | None:
    """Refuse a call outside the run's property, or beyond its caller's authority."""
    if scope.property_id is not None:
        presented = arguments.get("propertyId")
        if presented is None:
            if (target, action) not in PROPERTY_NEUTRAL:
                return (
                    "OUT_OF_SCOPE",
                    f"This run is about property {scope.property_id}. {action!r} "
                    "would reach beyond it, so it cannot be used here.",
                )
        # ``range_metrics``' chain-wide sentinel, ``_all``, lands here too: it is not
        # this run's property, so a property-scoped run cannot use it.
        elif presented != scope.property_id:
            return (
                "OUT_OF_SCOPE",
                f"This run is about property {scope.property_id}; {action!r} named "
                f"{presented!r}. Only this run's property can be read or changed.",
            )

    if scope.caller_groups is not None:
        allowed = TOOL_GROUPS.get((target, action), frozenset())
        if not scope.caller_groups & allowed:
            return (
                "NOT_PERMITTED_FOR_CALLER",
                f"The person who asked is in {sorted(scope.caller_groups) or 'no groups'}; "
                f"the platform allows {action!r} only for {sorted(allowed) or 'nobody'}. "
                "The copilot acts with the asker's authority, not more.",
            )

    return None


def _denial_reason(action: str, arguments: dict, scope: RunScope) -> tuple[str, str] | None:
    """``None`` if this call is humanly approved, else ``(code, why)``.

    Takes the *unprefixed* action, which is also what the ops console records on
    the approval, so the two are compared in the same vocabulary.

    The three codes are the same vocabulary ``tools/billing/handler.py`` uses, and
    they are distinguished on purpose. They land in the decision log's
    ``error_code`` column, where "the agent needs a human" and "the approvals table
    is unreachable" are different operational facts: one is the system working, the
    other is the system broken. Collapsing both into ``APPROVAL_REQUIRED`` would
    also tell the model to go file a proposal when no proposal can help.
    """
    token = scope.approval_token
    if not token:
        return (
            "APPROVAL_REQUIRED",
            f"{action!r} moves money and is Tier 2 (propose-and-confirm), so it "
            "cannot be executed on an agent's own authority. File a proposal for a "
            "human to approve in the ops console; their release starts a run that "
            "makes this call with the approval attached. Do not supply an "
            "approval_token yourself -- one written into the arguments is ignored.",
        )

    if not APPROVALS_TABLE:
        # Fail closed. An unconfigured table must never read as "approved" --
        # that would invert the guardrail into a rubber stamp.
        return (
            "APPROVAL_UNVERIFIABLE",
            "The approvals table is not configured, so this approval_token cannot "
            "be verified. Refusing the write.",
        )

    try:
        record = _approvals().get_item(
            TableName=APPROVALS_TABLE,
            Key={"approvalId": {"S": token}},
            # A human approving in the console and the agent retrying seconds
            # later is the normal path; an eventually-consistent read would
            # refuse a write that is genuinely approved.
            ConsistentRead=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, and say why
        logger.exception("approvals lookup failed")
        return (
            "APPROVAL_UNVERIFIABLE",
            f"Could not verify the approval_token: {type(exc).__name__}: {exc}. "
            "Refusing the write.",
        )

    item = record.get("Item")
    if not item:
        return (
            "APPROVAL_INVALID",
            "That approval_token does not exist, or it has already expired.",
        )

    status = (item.get("status") or {}).get("S")
    if status != "APPROVED":
        return (
            "APPROVAL_INVALID",
            f"That approval is in state {status!r}, not APPROVED.",
        )

    # A record with no action is refused, not treated as approving anything: the
    # console always records one, so absence means the record is not what it claims.
    granted_for = (item.get("action") or {}).get("S")
    if granted_for != action:
        return (
            "APPROVAL_MISMATCH",
            f"That approval was granted for {granted_for!r}, not {action!r}. An "
            "approval authorizes one specific action.",
        )

    expires_at = (item.get(TTL_ATTRIBUTE) or {}).get("N")
    if expires_at and float(expires_at) <= time.time():
        return "APPROVAL_INVALID", "That approval has expired. Ask for a fresh one."

    # The approval is for one hotel's money, and so is this run.
    approved_property = (item.get("propertyId") or {}).get("S")
    if approved_property and scope.property_id and approved_property != scope.property_id:
        return (
            "APPROVAL_MISMATCH",
            f"That approval was filed for property {approved_property!r}, but this run "
            f"is about {scope.property_id!r}.",
        )

    return _binding_mismatch(action, arguments, item)


#: What a Tier-2 approval binds, per action: the field the ops console recorded on the
#: approval -> the tool argument that must equal it. Every argument a proposal
#: captures is here, not just the target and amount. ``points`` was missing once,
#: so an approval for a 100-point loyalty credit would have released 100,000.
#: Mirrored in tools/billing/handler.py, and both must agree with the console's
#: GATED_ACTIONS (infra/lambdas/console/approvals/index.py).
BOUND_ARGUMENTS: dict[str, dict[str, str]] = {
    "post_charge": {"folioId": "folioId", "amount": "amount", "description": "description"},
    "void_folio": {"folioId": "folioId", "voidReason": "reason"},
    "adjust_loyalty": {"guestId": "guestId", "points": "points", "adjustReason": "reason"},
}

#: Compared as numbers, so 40, 40.0 and "40" are one amount and 4000 is not.
NUMERIC_FIELDS = frozenset({"amount", "points"})

#: Arguments a call may carry beyond the bound ones: the run's property, which the
#: scope check has already pinned, and the approval itself.
ALWAYS_ALLOWED = frozenset({"propertyId", "approval_token"})

#: Arguments a proposal does not capture, accepted only at their default. Anything
#: else the approval did not name -- a back-dated chargeDate, an ADJUSTMENT instead of
#: a SERVICE charge -- is a decision no human made, so it is refused.
UNCAPTURED_DEFAULTS = {"chargeType": "SERVICE"}


def _same(field: str, approved: str, presented: object) -> bool:
    if field in NUMERIC_FIELDS:
        from decimal import Decimal, InvalidOperation

        try:
            return Decimal(str(presented)) == Decimal(approved)
        except (InvalidOperation, ValueError, TypeError):
            return False
    return isinstance(presented, str) and presented == approved


def _binding_mismatch(action: str, arguments: dict, item: dict) -> tuple[str, str] | None:
    """Check the approval against *everything* this call does, not just what it is.

    Matching on the action alone is not enough, and the gap is not subtle: an
    approval to post a $40 late-checkout charge would authorize a $40 charge on any
    folio in the chain, because ``post_charge == post_charge``. Matching the target
    and amount but not the rest is not enough either -- that was the state of this
    function until a security review found that a loyalty approval never bound its
    ``points``, and that the approver was never shown them.

    So every captured field must be recorded *and* match, and no argument the
    approval did not capture may be added. A record missing a field it should carry
    is refused rather than treated as "unbound": the console has written every one
    of them since filing-time validation existed, so absence means a malformed or
    pre-dating record, and the safe reading of that is "not approved".
    """
    bound = BOUND_ARGUMENTS.get(action)
    if bound is None:
        return ("APPROVAL_MISMATCH", f"No approval binding is defined for {action!r}.")

    for field, argument in bound.items():
        attribute = item.get(field) or {}
        approved = attribute.get("S", attribute.get("N"))
        if approved is None:
            return (
                "APPROVAL_INCOMPLETE",
                f"That approval does not record {field}, so this call cannot be checked "
                "against what a human agreed to. File a fresh proposal.",
            )
        presented = arguments.get(argument)
        if not _same(field, approved, presented):
            return (
                "APPROVAL_MISMATCH",
                f"That approval is bound to {argument}={approved!r}, but this call "
                f"presents {presented!r}. A human approved exactly what was on the "
                "proposal -- not a starting point.",
            )

    for argument, value in arguments.items():
        if argument in ALWAYS_ALLOWED or argument in bound.values():
            continue
        if argument in UNCAPTURED_DEFAULTS and value == UNCAPTURED_DEFAULTS[argument]:
            continue
        return (
            "APPROVAL_MISMATCH",
            f"{argument!r} was not part of the approval, so it cannot be set here. File "
            "a proposal that names it.",
        )

    return None


def _refusal(
    call_id: object, tool_name: str, code: str, reason: str, *, batched: bool
) -> dict:
    """A short-circuit response the model reads as a failed tool call."""
    error_result = {
        "jsonrpc": "2.0",
        "id": call_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "success": False,
                            "error": {
                                "code": code,
                                "message": reason,
                                "details": {"tool": tool_name, "tier": 2},
                            },
                        }
                    ),
                }
            ],
            # The distinction that matters: an errored *result*, not a JSON-RPC
            # error. The model gets a tool failure it can reason about instead of
            # a transport exception that ends the turn.
            "isError": True,
        },
    }
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                # 200 is correct: the MCP call was well-formed and the protocol
                # exchange succeeded. The *tool* refused.
                "statusCode": 200,
                "body": [error_result] if batched else error_result,
            }
        },
    }
