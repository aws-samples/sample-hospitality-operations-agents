# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQS consumer that invokes the deployed agent Runtime, then records the answer.

Every unattended run in the system arrives here: the four EventBridge Scheduler
cadences (A1, A2, A4, A5) and the three reactive rules on the foundation's bus all
target one queue, and this function drains it. One durable path, one dead-letter
queue, one place to look when a 3 a.m. run did not happen.

Why a queue at all, when Scheduler and EventBridge can both invoke a Lambda
directly
----------------------------------------------------------------------------
Because an agent run is not a millisecond of compute. The Layer 3 runs measured
30-210 seconds, and a night audit is longer. A direct invocation that fails has
EventBridge's retry semantics and nowhere to land; a queued one has a visibility
timeout, a receive count, and a DLQ that still holds the message tomorrow morning.
The messages are also small JSON payloads a human can read in the console, which
matters the first time a schedule fires and produces something surprising.

What this function is not
-------------------------
It is not an agent, and it holds no foundation credentials. It cannot read a
reservation, resolve a property, or enrich a payload -- every path to the
foundation goes through the Gateway tool plane, and duplicating a Cognito identity
here to "just look one thing up" would put a second, unaudited door in a system
whose whole claim is that there is exactly one. So the payload it receives must
already be complete. EventBridge input transformers build them (see
``orchestration_stack``), which keeps event shapes out of this file entirely.

The run-summary row
-------------------
The Gateway's response interceptor logs every *tool call*, which is the trajectory
but not the conclusion -- it never sees the orchestrator's prose, so it writes no
``recommendation``. For an interactive run that is fine, because a human is
reading the stream. For a scheduled advisory run it is not: A4's 3 a.m.
night-audit assessment is the entire product of the run, and without a row it is
written to a CloudWatch log and then effectively deleted. So this function writes
one summary row per run carrying the answer, which is also what Phase 4's
run-history pane reads and what Phase 5 scores ``human_override`` against.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

RUNTIME_ARN = os.environ["RUNTIME_ARN"]

#: The named endpoint, not DEFAULT. Scheduled and reactive runs execute whatever
#: the ops console executes, on purpose: a schedule that silently ran a different
#: build than the one a human tested is a class of incident with no good debugging
#: story.
ENDPOINT_NAME = os.environ.get("ENDPOINT_NAME", "production")

DECISIONS_TABLE = os.environ.get("DECISIONS_TABLE", "")

#: The agent's prose is a few hundred to a few thousand characters. The cap exists
#: so a runaway answer cannot push the item past DynamoDB's 400 KB limit and lose
#: the whole row -- a truncated recommendation is still evidence; a failed PutItem
#: is not. Truncation is marked in the text so nobody reads a cut-off sentence as
#: the agent's complete conclusion.
MAX_RECOMMENDATION_CHARS = int(os.environ.get("MAX_RECOMMENDATION_CHARS", "30000"))

#: An agent run is minutes long, and the SSE stream is idle while the model
#: thinks. botocore's 60s default read timeout would sever a working run.
_AGENTCORE = boto3.client(
    "bedrock-agentcore",
    config=Config(
        read_timeout=870,
        connect_timeout=15,
        # The Runtime is invoked exactly once per message. A botocore-level retry
        # would start a *second* agent run -- duplicating writes the first one may
        # already have made -- so retries are the queue's job, where a redrive is
        # visible and bounded, and not this client's.
        retries={"max_attempts": 0},
    ),
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
    """Run one queued invocation per record, reporting failures individually.

    ``batchItemFailures`` rather than raising: the event source mapping is
    configured with a batch size of 1 today, but a partial-batch response is the
    shape that stays correct if that ever changes. Raising would redrive the
    successful runs too, and an agent run is not free to repeat.
    """
    failures: list[dict[str, str]] = []

    for record in (event or {}).get("Records") or []:
        message_id = record.get("messageId", "unknown")
        try:
            _run(record)
        except Exception:  # noqa: BLE001 - one bad message must not stop the batch
            logger.exception("invocation failed for message %s", message_id)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


def _run(record: dict) -> None:
    payload = _payload_of(record)
    started = datetime.now(timezone.utc)

    logger.info(
        "invoking runtime run=%s trigger=%s property=%s date=%s",
        payload["runId"],
        payload["trigger"],
        payload.get("propertyId") or "(chain-wide)",
        payload["operatingDate"],
    )

    response = _AGENTCORE.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=ENDPOINT_NAME,
        runtimeSessionId=_session_id(payload["runId"]),
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="text/event-stream",
    )

    answer, delegations, stop_reason, usage, error = _consume(response)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    logger.info(
        "run %s finished in %.1fs stop=%s delegations=%s error=%s",
        payload["runId"],
        elapsed,
        stop_reason,
        delegations,
        error,
    )

    # Written before the raise below, so a failed run leaves a record of having
    # been attempted. A run that vanished and a run that failed look identical
    # from the console otherwise, and only one of them needs investigating.
    _write_summary(
        payload=payload,
        answer=answer,
        delegations=delegations,
        stop_reason=stop_reason,
        usage=usage,
        error=error,
        started=started,
        elapsed=elapsed,
    )

    if error:
        # Surfaced to the queue so the message is retried and eventually lands in
        # the DLQ. The Runtime reporting INVALID_PAYLOAD will fail all three
        # attempts and then sit in the DLQ, which is the correct outcome: the
        # payload is a deployment bug, and a bug should be visible, not absorbed.
        raise RuntimeError(f"agent run {payload['runId']} reported: {error}")


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #


#: AgentCore's floor for a runtime session id. Not a guideline: a shorter value is
#: rejected by parameter validation before the request is even sent.
MIN_SESSION_ID = 33


def _session_id(run_id: str) -> str:
    """A valid runtime session id derived from the run id.

    Kept equal to the run id whenever it already qualifies, so the Runtime's session
    and the decision log's ``run_id`` are the same string and a trace can be followed
    across both without a mapping table.

    Padded when it is not. The earlier version passed the run id straight through on
    the reasoning that a uuid4 is 36 characters -- true of the ids this function
    generates, and false of the ones its callers supply. The ops console's approval
    route mints ``exec-<16 chars>``, which is 21, and every approved Tier-2 write
    failed parameter validation before reaching the Runtime: a human approved a
    charge, the token was minted, and the execution run silently never happened. The
    lesson is not "make the caller's ids longer" but "do not let a caller's id shape
    be load-bearing here".
    """
    if len(run_id) >= MIN_SESSION_ID:
        return run_id
    # Deterministic, so a retry of the same message reuses the same session rather
    # than starting a second one alongside the first.
    padding = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"{run_id}-{padding}"[:100]


def _payload_of(record: dict) -> dict:
    """Parse and complete one queued invocation payload.

    The defaults are filled *here* rather than left to the Runtime so that the
    summary row and the run itself cannot disagree about which date was reasoned
    over -- a schedule firing at 00:04 UTC and a row stamped with the previous day
    would be an audit trail that quietly lies.
    """
    try:
        payload = json.loads(record.get("body") or "{}")
    except ValueError as exc:
        raise ValueError(f"message body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("message body must be a JSON object")

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("message carries no 'prompt'")

    payload["prompt"] = prompt
    payload["trigger"] = (payload.get("trigger") or "schedule").strip()
    payload["runId"] = (payload.get("runId") or "").strip() or str(uuid.uuid4())
    payload["operatingDate"] = (
        (payload.get("operatingDate") or "").strip()
        or datetime.now(timezone.utc).date().isoformat()
    )
    # An empty string reaches run_context as falsy and becomes chain-wide anyway,
    # but dropping the key makes the logged payload honest about what was asked.
    if not (payload.get("propertyId") or "").strip():
        payload.pop("propertyId", None)

    return payload


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #


def _consume(response: dict) -> tuple[str, list[str], str | None, dict | None, str | None]:
    """Drain the Runtime's event stream.

    Returns ``(answer, delegations, stop_reason, usage, error)``. The stream must
    be read to completion even though nothing is watching it: abandoning it
    mid-run would drop the connection while the agent is still working, and the
    writes it had not yet made would silently not happen.
    """
    text_parts: list[str] = []
    delegations: list[str] = []
    stop_reason: str | None = None
    usage: dict | None = None
    error: str | None = None
    completed = False

    for event in _events(response):
        kind = event.get("type")
        if kind == "text":
            text_parts.append(event.get("delta") or "")
        elif kind == "delegation":
            if agent := event.get("agent"):
                delegations.append(agent)
        elif kind == "run_completed":
            completed = True
            stop_reason = event.get("stopReason")
            usage = event.get("usage")
            # The completion event carries the full text. Preferred over the
            # accumulated deltas when present, because a dropped frame would
            # otherwise produce an answer with a hole in it and no sign of one.
            if full := event.get("text"):
                text_parts = [full]
        elif kind == "error":
            error = f"{event.get('code')}: {event.get('message')}"

    answer = "".join(text_parts).strip()
    if not completed and not error:
        # The stream ended without the terminal event. Whatever the agent did or
        # did not do, this run cannot be reported as having finished.
        error = "stream ended without a run_completed event"

    return answer, delegations, stop_reason, usage, error


def _events(response: dict) -> Iterator[dict]:
    """Yield the decoded events, whichever shape the Runtime answered in.

    ``main.py``'s entrypoint is an async generator, so the Runtime answers
    ``text/event-stream``. The non-streaming branch is here because a future
    entrypoint that returns a plain dict would otherwise be read as an empty run
    -- silently, and only on the unattended path.
    """
    body = response.get("response")
    if body is None:
        return

    if "event-stream" not in (response.get("contentType") or ""):
        raw = body.read()
        try:
            decoded = json.loads(raw or b"null")
        except ValueError:
            logger.warning("non-stream response was not JSON: %r", raw[:200])
            return
        for event in decoded if isinstance(decoded, list) else [decoded]:
            if isinstance(event, dict):
                yield event
        return

    for line in body.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8", "replace").strip()
        if not decoded.startswith("data:"):
            # SSE comments and `event:`/`id:` fields. Not payload.
            continue
        frame = decoded[len("data:") :].strip()
        if not frame or frame == "[DONE]":
            continue
        try:
            event = json.loads(frame)
        except ValueError:
            logger.warning("unparseable stream frame: %s", frame[:200])
            continue
        if isinstance(event, dict):
            yield event


# --------------------------------------------------------------------------- #
# The summary row
# --------------------------------------------------------------------------- #


#: Approval tokens, as the ops console mints them (``apv-`` + urlsafe base64). Both
#: the prompt that carries one and any prose that repeats it are redacted before
#: storage -- see :func:`_redact`.
APPROVAL_TOKEN = re.compile(r"apv-[A-Za-z0-9_\-]{20,}")


def _redact(text: str | None) -> str:
    """Strip approval tokens from anything about to be stored or served.

    A released token has to reach the model, because the model is what writes tool
    arguments and ``approval_token`` is one. It does not have to survive the run.

    Two paths put it in the decision log, and both were live before this existed.
    The execution prompt contains the token by construction, and this function used
    to store that prompt verbatim. And on the first real end-to-end approval the
    model *repeated* the token in its answer -- "the charge was not posted... with
    your token apv-FRxH..." -- which then came back from ``GET /runs/{id}`` to any
    operator who could read that run, including the non-approvers who had just been
    refused the authority to mint one.

    ``main.py`` already filters tool inputs out of the event stream for exactly this
    reason, but a model's prose is not a tool input and no filter covered it. The
    token stays bound and short-lived either way, so this is defence in depth rather
    than the only thing standing between an operator and a spendable credential --
    but an audit log that quietly stores bearer tokens is not an audit log anyone
    should have to think twice about reading.
    """
    if not text:
        return text or ""
    return APPROVAL_TOKEN.sub("apv-[redacted]", text)


def _write_summary(
    *,
    payload: dict,
    answer: str,
    delegations: list[str],
    stop_reason: str | None,
    usage: dict | None,
    error: str | None,
    started: datetime,
    elapsed: float,
) -> None:
    """One row per unattended run, in the same table as the tool calls it made.

    Same partition key, so a single ``Query`` on ``run_id`` returns the whole run:
    the trajectory the interceptor logged, and this conclusion. ``agent`` is
    ``orchestrator`` because that is who wrote the prose -- the specialists that
    were consulted are in ``delegations``.

    Never raises. A run that did its work and then failed to file its paperwork
    has still done its work, and the audit trail is not more important than the
    operation it audits.
    """
    if not DECISIONS_TABLE:
        logger.warning("DECISIONS_TABLE is unset; run summary not recorded")
        return

    finished = datetime.now(timezone.utc)
    recommendation = _redact(answer) or "(the agent produced no text)"
    truncated = len(recommendation) > MAX_RECOMMENDATION_CHARS
    if truncated:
        recommendation = (
            recommendation[:MAX_RECOMMENDATION_CHARS] + "\n\n[truncated for storage]"
        )

    item: dict[str, dict[str, Any]] = {
        "run_id": {"S": payload["runId"]},
        # Sorts after every tool call the run made, which is chronologically true:
        # the conclusion is written last. Same `{iso}#{suffix}` shape the response
        # interceptor uses, so one sort key format covers the table.
        "event_id": {"S": f"{finished.isoformat()}#zz-summary"},
        "ts": {"S": finished.isoformat()},
        "property_id": {"S": payload.get("propertyId") or "_chain"},
        "agent": {"S": "orchestrator"},
        "trigger": {"S": payload["trigger"]},
        "operating_date": {"S": payload["operatingDate"]},
        "kind": {"S": "run_summary"},
        "prompt": {"S": _redact(payload["prompt"])},
        "recommendation": {"S": recommendation},
        "outcome": {"S": "failed" if error else "ok"},
        "started_at": {"S": started.isoformat()},
        "duration_seconds": {"N": f"{elapsed:.1f}"},
    }
    if truncated:
        item["recommendation_truncated"] = {"BOOL": True}
    if delegations:
        # Order preserved: it is the routing decision, and "A4 then A3" is a
        # different run from "A3 then A4".
        item["delegations"] = {"L": [{"S": name} for name in delegations]}
    if stop_reason:
        item["stop_reason"] = {"S": stop_reason}
    if error:
        item["error_code"] = {"S": error[:512]}
    if usage:
        item["usage"] = {
            "M": {k: {"N": str(v)} for k, v in usage.items() if isinstance(v, int)}
        }

    try:
        _decisions().put_item(TableName=DECISIONS_TABLE, Item=item)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("run summary write failed for %s", payload["runId"])
