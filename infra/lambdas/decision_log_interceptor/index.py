# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Gateway RESPONSE interceptor: write the decision log. Change nothing else.

``PATTERN_EXTENSION_GUIDE.md`` §3.4 asks for two different kinds of record, and this is the
second one. X-Ray traces answer *what happened* -- which span was slow, which
call threw. They cannot answer *was the agent right*, because the answer to that
arrives days later when a human either lets a decision stand or reverses it. So
every tool call an agent makes lands as a row in ``hotel-ops-agent-decisions``,
and the ops console later stamps ``human_override`` on the ones a person undid.
That column is the ground truth the Phase-5 evaluators score against; without it
online evaluation has no signal but the model's own opinion of itself.

This function is a pure observer. It returns the gateway's response byte-for-byte
and swallows every error it can produce, because the alternative is a logging
failure that breaks a room assignment. Audit is important; it is not more
important than the operation being audited.

What it can see, and what it therefore does not pretend to know
--------------------------------------------------------------
The interceptor sees one MCP exchange: the tool name, the arguments, and the
result envelope. It does *not* see the sub-agent's prose, so it writes no
``recommendation`` -- that field is filled by whoever files a Tier-2 proposal, in
the ops console API, where the prose actually exists. Inventing it here from the
tool arguments would produce an audit trail that reads like a record of reasoning
and is really a record of a JSON payload.

Arguments are stored as a hash, never verbatim. They carry guest names, folio
detail, and -- on a Tier-2 call -- the approval token a human just issued. The
hash is enough to tell "the same decision, recomputed" from "a different
decision", which is the only question the eval pipeline asks of it.

Correlation
-----------
``run_id``, ``property_id``, and the delegating sub-agent are not in the MCP
protocol; ``agents/gateway.py`` puts them in ``X-Hotel-Ops-*`` request headers,
which is why this interceptor is configured with ``pass_request_headers=True``.
So is the approval interceptor now -- it enforces the same headers rather than
merely recording them -- though this docstring said otherwise for as long as the
run's property reached the tools only as prompt text. Headers are read and never
logged: the same set carries the caller's SigV4 ``Authorization``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: Created by ``orchestration_stack`` in Phase 3. Referenced by name so this
#: interceptor can ship before that stack exists -- until it does, every write
#: fails, is logged, and changes nothing about the response.
DECISIONS_TABLE = os.environ.get("DECISIONS_TABLE", "")

OUTPUT_VERSION = "1.0"

#: Correlation headers set by ``agents/gateway.py``. Lower-cased on lookup, since
#: header case is not guaranteed to survive the trip.
RUN_ID_HEADER = "x-hotel-ops-run-id"
PROPERTY_HEADER = "x-hotel-ops-property-id"
AGENT_HEADER = "x-hotel-ops-agent"
DATE_HEADER = "x-hotel-ops-operating-date"
TRIGGER_HEADER = "x-hotel-ops-trigger"

#: Splits a Gateway tool name into ``(target, action)`` on any run of two or more
#: underscores. The deployed Gateway uses three (``arrivals___assign_room``);
#: parsing rather than assuming means a change in that width shows up as nothing at
#: all here instead of as an audit table that quietly stops recording writes.
TOOL_NAME = re.compile(r"^(?P<target>[A-Za-z0-9]+)_{2,}(?P<action>.+)$")

#: The eight ``(target, action)`` pairs that change state in the foundation.
#: Everything else is a read, and a read is not a decision -- it gets a row (the
#: trajectory matters for evaluation) but no ``action_taken``, because nothing was
#: done.
WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("arrivals", "assign_room"),
        ("arrivals", "check_in"),
        ("housekeeping", "assign_task"),
        ("housekeeping", "complete_task"),
        ("housekeeping", "inspect_task"),
        ("billing", "post_charge"),
        ("billing", "void_folio"),
        ("billing", "adjust_loyalty"),
    }
)

_dynamodb = None


def _decisions():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.client("dynamodb")
    return _dynamodb


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


def handler(event: dict, context: Any) -> dict:
    """Record one tool result, then hand the response back untouched."""
    mcp = (event or {}).get("mcp") or {}
    response = mcp.get("gatewayResponse") or {}

    try:
        _record(mcp, response)
    except Exception:  # noqa: BLE001 - an audit failure must not fail the call
        logger.exception("decision log write failed; response passed through")

    return _passthrough(response)


def _passthrough(response: dict) -> dict:
    """Echo the gateway's own response.

    ``statusCode`` and ``headers`` are only echoed when present. On a streaming
    response the gateway sends them with the first event and ignores them on the
    rest, so returning a fabricated ``200`` for a later chunk would be asserting
    something this invocation does not actually know.
    """
    transformed: dict[str, Any] = {"body": response.get("body")}
    if "statusCode" in response:
        transformed["statusCode"] = response["statusCode"]
    if response.get("headers") is not None:
        transformed["headers"] = response["headers"]
    return {
        "interceptorOutputVersion": OUTPUT_VERSION,
        "mcp": {"transformedGatewayResponse": transformed},
    }


def _record(mcp: dict, response: dict) -> None:
    request = mcp.get("gatewayRequest") or {}
    request_body = request.get("body")
    response_body = response.get("body")

    call = _tool_call(request_body)
    if call is None:
        # tools/list, initialize, ping, notifications. Protocol chatter, not a
        # decision.
        return

    tool_name, arguments = call

    if not _is_tool_result(response_body, request_body):
        # With streaming enabled this interceptor fires once per eligible event,
        # including server-initiated requests such as elicitation/create. Only the
        # event that actually carries the tool's result is the decision.
        logger.debug("skipping non-result event for %s", tool_name)
        return

    if not DECISIONS_TABLE:
        logger.warning(
            "DECISIONS_TABLE is unset; not logging %s. This is expected until "
            "orchestration_stack is deployed.",
            tool_name,
        )
        return

    headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
    outcome, error_code = _outcome_of(response_body)
    target, action = _split_tool_name(tool_name)
    now = datetime.now(timezone.utc)

    item: dict[str, dict[str, Any]] = {
        # A run with no correlation header is still worth logging -- it means
        # someone invoked the Gateway outside the agent graph, which is exactly
        # the kind of thing an audit table should show rather than discard.
        "run_id": {"S": headers.get(RUN_ID_HEADER) or "unattributed"},
        "event_id": {"S": f"{now.isoformat()}#{uuid.uuid4().hex[:8]}"},
        "ts": {"S": now.isoformat()},
        "property_id": {"S": headers.get(PROPERTY_HEADER) or "_unknown"},
        "agent": {"S": headers.get(AGENT_HEADER) or target or tool_name},
        "tool": {"S": tool_name},
        "inputs_hash": {"S": _inputs_hash(arguments)},
        "outcome": {"S": outcome},
    }
    if (target, action) in WRITE_TOOLS and outcome == "ok":
        # Only a *successful* write is an action taken. A refused Tier-2 charge
        # must not read as money moved. Stored unprefixed, because `tool` above
        # already holds the fully qualified name and the ops console's run history
        # reads "agent=arrivals action=assign_room" rather than repeating itself.
        item["action_taken"] = {"S": action}
    if error_code:
        item["error_code"] = {"S": error_code}
    if operating_date := headers.get(DATE_HEADER):
        item["operating_date"] = {"S": operating_date}
    if trigger := headers.get(TRIGGER_HEADER):
        item["trigger"] = {"S": trigger}

    # No condition expression. The gateway may retry this interceptor, so a
    # duplicate row is possible; the alternative -- a deterministic sort key --
    # would silently collapse a genuine second identical call into the first,
    # which is a worse lie than a double entry. run_id + tool + inputs_hash makes
    # duplicates collapsible downstream.
    _decisions().put_item(TableName=DECISIONS_TABLE, Item=item)
    logger.info(
        "decision run=%s agent=%s tool=%s outcome=%s",
        item["run_id"]["S"],
        item["agent"]["S"],
        tool_name,
        outcome,
    )


# --------------------------------------------------------------------------- #
# Reading the exchange
# --------------------------------------------------------------------------- #


def _tool_call(body: object) -> tuple[str, dict] | None:
    """``(toolName, arguments)`` for a ``tools/call`` request, else ``None``."""
    # A batch is not something AgentCore's client emits, and a response
    # interceptor sees one result at a time regardless, so the first tool call in
    # the request is the one this response belongs to.
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
        arguments = params.get("arguments")
        return name, arguments if isinstance(arguments, dict) else {}
    return None


def _is_tool_result(response_body: object, request_body: object) -> bool:
    """True when this event is the answer to the tool call, not a side channel.

    A JSON-RPC *response* carries ``result`` or ``error`` and echoes the request's
    ``id``. A server-initiated request (``elicitation/create``,
    ``sampling/createMessage``) carries ``method`` and an id of its own.
    """
    if isinstance(response_body, list):
        response_body = next((m for m in response_body if isinstance(m, dict)), None)
    if not isinstance(response_body, dict):
        return False
    if "result" not in response_body and "error" not in response_body:
        return False

    request_id = None
    if isinstance(request_body, dict):
        request_id = request_body.get("id")
    elif isinstance(request_body, list):
        first = next((m for m in request_body if isinstance(m, dict)), None)
        request_id = (first or {}).get("id")
    # An id we cannot compare is not grounds for dropping the row; the presence of
    # `result` already establishes this is a response.
    return request_id is None or response_body.get("id") == request_id


def _outcome_of(response_body: object) -> tuple[str, str | None]:
    """Classify the result, and pull out the foundation's error code if there is one.

    Three shapes matter. A JSON-RPC ``error`` is a protocol-level failure. A
    ``result`` with ``isError`` is a tool that ran and refused -- an approval gate
    denial arrives this way. And the tool's own text is the foundation's envelope,
    whose ``error.code`` is the most useful single field in the whole row: it
    distinguishes ``APPROVAL_REQUIRED`` from ``ForbiddenError`` from a 409.
    """
    if isinstance(response_body, list):
        response_body = next((m for m in response_body if isinstance(m, dict)), None)
    if not isinstance(response_body, dict):
        return "unknown", None

    if error := response_body.get("error"):
        code = error.get("code") if isinstance(error, dict) else None
        return "protocol_error", str(code) if code is not None else None

    result = response_body.get("result")
    if not isinstance(result, dict):
        return "unknown", None

    code = _envelope_error_code(result)
    if result.get("isError"):
        return "tool_error", code
    # A successful MCP call can still carry {"success": false} from the
    # foundation -- the tool layer returns that envelope verbatim on purpose.
    return ("failed", code) if code else ("ok", None)


def _envelope_error_code(result: dict) -> str | None:
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error")
            if isinstance(error, dict) and error.get("code"):
                return str(error["code"])
    return None


def _inputs_hash(arguments: dict) -> str:
    """Stable hash of the tool arguments. Never the arguments themselves."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _split_tool_name(tool_name: str) -> tuple[str, str]:
    """``"arrivals___assign_room"`` -> ``("arrivals", "assign_room")``.

    An unprefixed name yields ``("", name)``. The caller falls back to the whole
    name for the ``agent`` column in that case, and the empty target matches
    nothing in :data:`WRITE_TOOLS` -- so an unrecognizable name is recorded as
    observed rather than guessed at, and never as an action taken.
    """
    match = TOOL_NAME.match(tool_name)
    return (match["target"], match["action"]) if match else ("", tool_name)
