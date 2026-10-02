# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Tier-2 approval queue: where a human releases money movement.

``hotel-operations-agent.md`` §7 draws one line the system must never cross -- an
agent may reorganize work freely and must never move money on its own. Everything
else in this project is machinery; this endpoint is the place a person actually
stands on that line.

Four routes:

* ``GET /approvals`` -- the queue, newest first, filtered by status.
* ``POST /approvals`` -- file a proposal. An operator reading A3's recommendation
  turns it into a pending request, naming exactly what is to be done.
* ``POST /approvals/{id}/approve`` -- release it. Mints the token and re-invokes
  the agent to execute with it.
* ``POST /approvals/{id}/reject`` -- refuse it, with a reason.

An approval is narrow on purpose
--------------------------------
The record binds the token to an **action and its target and its amount**, not just
to an action. Without the target, an approval to post a $40 late-checkout charge on
one folio would authorize a $40 charge on any folio in the chain -- the gate would
compare ``post_charge`` to ``post_charge`` and let it through. The gates enforce
every field recorded here, so a released approval is a permission to do one thing
to one folio for one amount, and nothing else.

The token is not consumed by being spent
----------------------------------------
Both gates read the approval without consuming it, deliberately: the Gateway may
retry an interceptor invocation, and a gate that burned the token on a retry would
refuse a write the human did approve. So the token stays valid until it expires,
and the containment is the narrowness above plus a short TTL -- not single use.
That is a real property of the design and it is stated here rather than left for
someone to discover.

Who may do what
---------------
Filing a proposal and reading the queue need only an authenticated account.
Approving needs ``Admin`` or ``Manager``, and someone other than the proposer. Anyone
may reject: refusing to move money is never the dangerous direction.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from hotel_console.api import ApiError, body_of, handler_for, int_param, path_param, query_param, table

CHAT_QUEUE_URL = os.environ["CHAT_QUEUE_URL"]

#: How long a released approval stays spendable. Short, because the token is not
#: single-use: this window *is* the containment. Long enough that the follow-up
#: agent run -- which takes a minute or two -- comfortably finishes inside it.
APPROVAL_TTL_SECONDS = int(os.environ.get("APPROVAL_TTL_SECONDS", "900"))

#: How long an unreleased proposal survives. It has no power, but a queue full of
#: last month's untouched proposals is a queue nobody reads.
PENDING_TTL_SECONDS = int(os.environ.get("PENDING_TTL_SECONDS", str(7 * 24 * 3600)))

#: The three Tier-2 tools: what each one's target field is called, and which of the
#: tool's other arguments a proposal must capture.
#:
#: The ``requires`` list is not paperwork. On the first end-to-end approval the agent
#: was released to post a charge, went to execute it, found that ``post_charge`` needs
#: a ``description`` the proposal had never captured -- and stopped to ask a human
#: what to write. That was the right call by the agent: inventing a line-item
#: description on someone's folio is not a gap to paper over. But it meant a human
#: approved money movement and the loop stalled waiting on a second answer. So a
#: proposal now has to carry everything the tool needs before it can be filed, and
#: the execution prompt passes all of it.
#:
#: ``target`` is separately load-bearing: the gates bind the token to it, so an action
#: not listed here could never be enforced and is refused at filing time.
GATED_ACTIONS: dict[str, dict] = {
    "post_charge": {
        "target": "folioId",
        # `amount` is also bound by the gates; `description` is not, because it
        # cannot authorize anything -- it just has to exist so the agent is not
        # guessing.
        "requires": ["amount", "description"],
    },
    "void_folio": {
        "target": "folioId",
        # The tool's own `reason` argument, which the foundation records on the void.
        # Distinct from the proposal's `reason`, which is what the approver reads.
        "requires": ["voidReason"],
    },
    "adjust_loyalty": {
        "target": "guestId",
        "requires": ["points", "adjustReason"],
    },
}

#: Proposal field -> the tool argument it becomes. Two of them are renamed because
#: ``reason`` is already taken by the proposal's own human-facing justification, and
#: one field meaning two things is how the wrong text ends up on a guest's folio.
TOOL_ARGUMENT = {
    "voidReason": "reason",
    "adjustReason": "reason",
}

STATUSES = ("PENDING", "APPROVED", "REJECTED")

_sqs = boto3.client("sqs")


def route(event: dict, caller):
    method = event.get("httpMethod")
    resource = event.get("resource") or ""

    if method == "GET":
        return _list(event, caller)
    if method == "POST" and resource.endswith("/approve"):
        return _decide(event, caller, approve=True)
    if method == "POST" and resource.endswith("/reject"):
        return _decide(event, caller, approve=False)
    if method == "POST":
        return _propose(event, caller)
    raise ApiError(405, "METHOD_NOT_ALLOWED", f"{method} {resource} is not a route.")


# --------------------------------------------------------------------------- #
# GET /approvals
# --------------------------------------------------------------------------- #


def _list(event: dict, caller) -> dict:
    status = (query_param(event, "status", "PENDING") or "").upper()
    if status not in STATUSES and status != "ALL":
        raise ApiError(
            400, "INVALID_STATUS", f"status must be one of {STATUSES} or ALL."
        )
    limit = int_param(event, "limit", 50, maximum=200)
    approvals = table("APPROVALS_TABLE")

    items: list[dict] = []
    for wanted in STATUSES if status == "ALL" else [status]:
        response = approvals.query(
            IndexName="status-createdAt-index",
            KeyConditionExpression=Key("status").eq(wanted),
            ScanIndexForward=False,
            Limit=limit,
        )
        items.extend(response.get("Items", []))

    # Filtered after the read: the index is keyed on status, not property, and a
    # per-property index on a table this small would cost more than it saves.
    visible = [i for i in items if _readable(caller, i)]
    visible.sort(key=lambda i: i.get("createdAt", ""), reverse=True)

    return {
        "approvals": [_public(i) for i in visible[:limit]],
        "count": len(visible[:limit]),
        "status": status,
        "youMayApprove": caller.may_approve,
    }


def _readable(caller, item: dict) -> bool:
    property_id = item.get("propertyId")
    if not caller.property_id:
        return caller.is_chain_level
    return property_id == caller.property_id


def _public(item: dict) -> dict:
    """The queue row, **without the token.**

    ``approvalId`` *is* the token the agent presents, so it is withheld from every
    list response. It is returned exactly once, to the person who released it, from
    the approve route. A queue endpoint that handed out spendable tokens to everyone
    who could read the queue would make the approver group meaningless.
    """
    return {
        "id": item.get("proposalId"),
        "action": item.get("action"),
        "status": item.get("status"),
        "propertyId": item.get("propertyId"),
        "folioId": item.get("folioId"),
        "guestId": item.get("guestId"),
        "amount": item.get("amount"),
        "reason": item.get("reason"),
        "runId": item.get("runId"),
        "proposedBy": item.get("proposedBy"),
        "createdAt": item.get("createdAt"),
        "decidedBy": item.get("decidedBy"),
        "decidedAt": item.get("decidedAt"),
        "decisionNote": item.get("decisionNote"),
        "expiresAt": item.get("expiresAt"),
        "executionRunId": item.get("executionRunId"),
        # Everything that will be sent to the tool, so an approver reads the same
        # facts the agent will act on rather than a summary of them.
        "arguments": {
            TOOL_ARGUMENT.get(field, field): item.get(field)
            for field in sorted(
                set().union(*(set(s["requires"]) for s in GATED_ACTIONS.values()))
            )
            if item.get(field) is not None
        },
    }


# --------------------------------------------------------------------------- #
# POST /approvals
# --------------------------------------------------------------------------- #


def _propose(event: dict, caller) -> dict:
    body = body_of(event)

    action = (body.get("action") or "").strip()
    if action not in GATED_ACTIONS:
        raise ApiError(
            400,
            "NOT_A_GATED_ACTION",
            f"action must be one of {sorted(GATED_ACTIONS)}. Anything else is Tier 1 "
            "or Tier 3 and executes without an approval -- filing one for it would "
            "create a token that no gate will ever check.",
        )

    spec = GATED_ACTIONS[action]
    target_field = spec["target"]
    target = (body.get(target_field) or "").strip()
    if not target:
        raise ApiError(
            400,
            "MISSING_TARGET",
            f"{action} needs {target_field}. The approval is bound to it: without a "
            f"target this token would authorize {action} against anything.",
        )

    extras = _required_arguments(action, spec, body)

    reason = (body.get("reason") or "").strip()
    if not reason:
        raise ApiError(
            400,
            "MISSING_REASON",
            "State what is being proposed and why. This is what the approver reads "
            "before releasing money, and what the audit log keeps afterwards.",
        )

    property_id = caller.scope(body.get("propertyId"))
    if not property_id:
        raise ApiError(
            400,
            "MISSING_PROPERTY",
            "propertyId is required: an approval is always about one hotel's money.",
        )

    amount = _amount(body.get("amount"), action)
    now = datetime.now(timezone.utc)

    item = {
        # Two ids, and the difference matters. `approvalId` is the secret the agent
        # presents and the table's partition key; `proposalId` is the public handle
        # the console uses in URLs. Keying on the secret is what lets the gate verify
        # with a single GetItem and no index.
        "approvalId": _mint_token(),
        "proposalId": f"prop-{secrets.token_urlsafe(9)}",
        "status": "PENDING",
        "action": action,
        target_field: target,
        "propertyId": property_id,
        "reason": reason,
        "proposedBy": caller.email or caller.subject,
        "createdAt": now.isoformat(),
        "expiresAt": int(time.time()) + PENDING_TTL_SECONDS,
    }
    if amount is not None:
        item["amount"] = amount
    item.update(extras)
    if run_id := (body.get("runId") or "").strip():
        # Links the proposal back to the run that recommended it, so an approver can
        # read the agent's full reasoning rather than this one-line summary.
        item["runId"] = run_id

    table("APPROVALS_TABLE").put_item(Item=item)
    return {"approval": _public(item), "note": "Pending. Not spendable until approved."}


def _required_arguments(action: str, spec: dict, body: dict) -> dict:
    """Every other tool argument this action needs, validated at filing time.

    Refused here rather than discovered at execution time, which is the whole point:
    at filing time a missing field is a form error, and after approval it is a human
    who has already released money waiting on a second question.
    """
    extras: dict = {}
    for field in spec["requires"]:
        if field == "amount":
            continue  # handled separately, because the gates bind it
        raw = body.get(field)
        if field == "points":
            try:
                extras[field] = int(raw)
            except (TypeError, ValueError):
                raise ApiError(
                    400,
                    "MISSING_ARGUMENT",
                    "adjust_loyalty needs points: a whole number, negative to deduct.",
                ) from None
            if extras[field] == 0:
                raise ApiError(
                    400, "INVALID_ARGUMENT", "A zero-point adjustment does nothing."
                )
            continue
        text = (raw or "").strip() if isinstance(raw, str) else ""
        if not text:
            raise ApiError(
                400,
                "MISSING_ARGUMENT",
                f"{action} needs {field}. It is passed straight to the hotel platform "
                f"and appears on the guest's record, so a human writes it -- the agent "
                f"must not invent it, and if it is absent the agent will stop and ask "
                f"rather than guess.",
            )
        extras[field] = text
    return extras


def _amount(raw, action: str):
    from decimal import Decimal, InvalidOperation

    if raw in (None, ""):
        if action == "post_charge":
            raise ApiError(
                400,
                "MISSING_AMOUNT",
                "post_charge must name its amount. The gate compares it against the "
                "charge the agent tries to post, so an unbound amount would let an "
                "approval for $40 release $4,000.",
            )
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ApiError(400, "INVALID_AMOUNT", f"amount must be a number: {raw!r}") from exc
    if action == "post_charge" and value <= 0:
        raise ApiError(400, "INVALID_AMOUNT", "A charge amount must be positive.")
    return value


def _mint_token() -> str:
    """The approval token.

    ``secrets``, not ``uuid4``: this string is a bearer credential for moving money,
    and it must be unguessable rather than merely unique. 32 bytes of urlsafe base64
    is ~256 bits.
    """
    return f"apv-{secrets.token_urlsafe(32)}"


# --------------------------------------------------------------------------- #
# POST /approvals/{id}/approve | /reject
# --------------------------------------------------------------------------- #


def _decide(event: dict, caller, *, approve: bool) -> dict:
    proposal_id = path_param(event, "id")
    body = body_of(event)
    note = (body.get("note") or "").strip()

    item = _by_proposal_id(proposal_id)
    if not _readable(caller, item):
        raise ApiError(403, "OUT_OF_SCOPE", "That approval belongs to another property.")
    if item.get("status") != "PENDING":
        raise ApiError(
            409,
            "ALREADY_DECIDED",
            f"That proposal is already {item.get('status')}, decided by "
            f"{item.get('decidedBy')}. Re-deciding it would overwrite someone's "
            "judgment; file a fresh proposal instead.",
        )

    if not approve:
        return _reject(item, caller, note)

    caller.require_approver(item.get("action") or "this action")
    # Two people, always. Without this an approver could file a proposal and release
    # it themselves, and the approval would record one person's decision twice.
    # Compared on both identifiers because proposedBy is written as email-or-subject.
    proposer = item.get("proposedBy")
    if proposer and proposer in {caller.email, caller.subject}:
        raise ApiError(
            403,
            "SELF_APPROVAL",
            "You filed this proposal, so someone else has to release it. Moving money "
            "needs two people: the one who asks and the one who agrees.",
        )
    if not note:
        raise ApiError(
            400,
            "MISSING_NOTE",
            "Say why you are releasing this. The agent's reason records what it "
            "wanted; this records that a named human agreed, which is the only part "
            "of the chain that carries authority.",
        )
    return _approve(item, caller, note)


def _reject(item: dict, caller, note: str) -> dict:
    now = datetime.now(timezone.utc)
    approvals = table("APPROVALS_TABLE")
    approvals.update_item(
        Key={"approvalId": item["approvalId"]},
        UpdateExpression=(
            "SET #s = :status, decidedBy = :who, decidedAt = :at, decisionNote = :note"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "REJECTED",
            ":who": caller.email or caller.subject,
            ":at": now.isoformat(),
            ":note": note or "(no note)",
            ":pending": "PENDING",
        },
        # Loses a race rather than overwriting a colleague's decision.
        ConditionExpression="#s = :pending",
    )
    return {
        "id": item.get("proposalId"),
        "status": "REJECTED",
        "note": (
            "Rejected. The token was never issued, so the gates will refuse this "
            "write as APPROVAL_INVALID if it is attempted."
        ),
    }


def _approve(item: dict, caller, note: str) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = int(time.time()) + APPROVAL_TTL_SECONDS

    table("APPROVALS_TABLE").update_item(
        Key={"approvalId": item["approvalId"]},
        UpdateExpression=(
            "SET #s = :status, decidedBy = :who, decidedAt = :at, "
            "decisionNote = :note, expiresAt = :ttl"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "APPROVED",
            ":who": caller.email or caller.subject,
            ":at": now.isoformat(),
            ":note": note,
            # Overwrites the pending TTL with the much shorter spendable window.
            ":ttl": expires_at,
            ":pending": "PENDING",
        },
        ConditionExpression="#s = :pending",
    )

    execution_run_id = _dispatch_execution(item, caller, note)

    return {
        "id": item.get("proposalId"),
        "status": "APPROVED",
        "expiresAt": expires_at,
        "expiresInSeconds": APPROVAL_TTL_SECONDS,
        "executionRunId": execution_run_id,
        "note": (
            "Released. The agent has been re-invoked to execute it, with the approval "
            f"attached out of band; watch run {execution_run_id}. The approval is bound "
            f"to {item.get('action')} on this exact target and amount, and expires "
            f"in {APPROVAL_TTL_SECONDS // 60} minutes whether or not it is used."
        ),
    }


def _dispatch_execution(item: dict, caller, note: str) -> str:
    """Re-invoke the agent so it performs the write it proposed.

    The token goes in the payload's ``approvalToken`` field, **not the prompt**. The
    Runtime keeps it out of band and sends it to the Gateway as a header, where the
    request interceptor attaches it to the one gated call. It used to be spelled out
    in the prompt, on the theory that the model writes tool arguments and so had to
    see it -- which meant AgentCore Memory stored it, traces recorded it, and on the
    first real approval the model repeated it in its answer. The model never needed
    it: it only needs to know *what* to execute.

    The prompt names the action and the target explicitly rather than saying "do
    what you proposed". A run that has to recall its own earlier proposal from
    memory is a run that can misremember which folio it was about.
    """
    action = item.get("action")
    spec = GATED_ACTIONS.get(action or "", {"target": "folioId", "requires": []})
    target_field = spec["target"]
    target = item.get(target_field)

    # Every argument, named, so the agent has nothing to fill in from memory or
    # imagination. Rendered as `name=value` pairs rather than prose for the same
    # reason.
    # propertyId first: the Gateway refuses any call naming a property other than
    # this run's, and this run is pinned to the approval's.
    parts = [f"propertyId={item.get('propertyId')}", f"{target_field}={target}"]
    for field in spec["requires"]:
        value = item.get(field)
        if value is not None:
            parts.append(f"{TOOL_ARGUMENT.get(field, field)}={value!r}")
    detail = ", ".join(parts)

    prompt = (
        f"A human ({caller.email or caller.subject}) has approved your proposal to "
        f"{action}. Their note: {note}. "
        f"Execute it now with exactly these arguments and no others: {detail}. "
        f"The approval itself is attached for you; do not supply an approval_token. "
        f"Every value you need is here -- do not substitute, round, or invent any of "
        f"them, and do not stop to ask: the human has already decided and is not "
        f"waiting for a second question. The approval is bound to this action, this "
        f"target and this amount, so changing any of them will simply be refused. "
        f"Perform no other write in this run. Report what the platform returned, "
        f"including a failure."
    )

    run_id = f"exec-{secrets.token_urlsafe(12)}"
    payload = {
        "prompt": prompt,
        "trigger": "chat",
        "runId": run_id,
        "propertyId": item.get("propertyId"),
        "callerGroups": list(caller.groups),
        # Out of band: see the docstring. The invoker forwards the payload whole and
        # writes only named fields to the decision log, none of them this one.
        "approvalToken": item["approvalId"],
    }
    _sqs.send_message(QueueUrl=CHAT_QUEUE_URL, MessageBody=json.dumps(payload))

    table("APPROVALS_TABLE").update_item(
        Key={"approvalId": item["approvalId"]},
        UpdateExpression="SET executionRunId = :r",
        ExpressionAttributeValues={":r": run_id},
    )
    return run_id


def _by_proposal_id(proposal_id: str) -> dict:
    """Resolve the public handle to the record.

    A scan, and that is the right call: the table holds pending approvals with a
    seven-day TTL, so it is tens of rows, and the alternative -- a second GSI on
    ``proposalId`` -- would cost storage and write capacity forever to save
    milliseconds on a human's button press. The partition key is the *secret*, which
    is what the gate needs to be fast; the console can afford to look things up the
    slow way.
    """
    approvals = table("APPROVALS_TABLE")
    kwargs = {
        "FilterExpression": "proposalId = :p",
        "ExpressionAttributeValues": {":p": proposal_id},
    }
    while True:
        response = approvals.scan(**kwargs)
        for item in response.get("Items", []):
            return item
        token = response.get("LastEvaluatedKey")
        if not token:
            raise ApiError(404, "APPROVAL_NOT_FOUND", f"No proposal {proposal_id}.")
        kwargs["ExclusiveStartKey"] = token


handler = handler_for(route)
