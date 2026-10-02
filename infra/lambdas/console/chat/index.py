# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``POST /chat`` -- start a copilot run as the signed-in operator.

Why this enqueues instead of streaming
--------------------------------------
The plan called for ``POST /chat`` to stream from the Runtime with
``InvokeAgentRuntimeWithResponseStream``. It cannot, behind API Gateway: the
default REST integration timeout is 29 seconds and the runs this system actually
produces take 30 to 650 seconds. Every real chat message would time out. (The REST
quota is adjustable, but holding a browser connection open for a ten-minute agent
run is fragile regardless, and raising an account-level quota in an account shared
with a live platform is not a decision this project should make on its own.)

So a chat message takes the same path as every other invocation: onto a queue, into
``agent_invoker``, out through the decision log. Three things fall out of that,
and they are the reason this is a better design rather than only a workable one:

* **One durable path.** A chat run gets the same DLQ, the same retry bound and the
  same run-summary row as a scheduled run. There is one place to look when
  something did not happen.
* **Progress is already being recorded.** The Gateway's response interceptor
  writes a row per tool call as the run happens, so ``GET /runs/{id}`` can show
  "arrivals_agent called list_rooms" while the model is still working. For an ops
  console that trail is more useful than tokens arriving one at a time -- it is
  the audit record, live.
* **Nothing new to secure.** Streaming would have needed a Lambda Function URL,
  which cannot use a Cognito authorizer, so the token would have had to be
  verified in our own code behind a publicly invocable endpoint.

A separate queue from the scheduled work
----------------------------------------
Chat has its own queue, consumed by the same invoker. Sharing the scheduled queue
would let a human's question sit behind a ten-minute A5 portfolio review, which is
the one place in this system where latency is felt by a person.

Authority
---------
``callerGroups`` comes from the verified ID token, never from the body. That is
what ``hotel-operations-agent.md`` §6's copilot rule needs: the run carries the
operator's real authority, so an agent cannot be talked into acting with more
privilege than the human who asked.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from hotel_console.api import ApiError, body_of, handler_for, table

CHAT_QUEUE_URL = os.environ["CHAT_QUEUE_URL"]

#: Long enough for a real operating question, short enough that the prompt cannot
#: be used to smuggle a wall of text past the model's own instructions.
MAX_PROMPT_CHARS = 4000

logger = logging.getLogger()

_sqs = boto3.client("sqs")


def route(event: dict, caller) -> dict:
    body = body_of(event)

    prompt = (body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        raise ApiError(400, "MISSING_PROMPT", "Send a 'prompt' to ask the agents.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ApiError(
            400,
            "PROMPT_TOO_LONG",
            f"Prompts are limited to {MAX_PROMPT_CHARS} characters; this one is "
            f"{len(prompt)}.",
        )

    # May narrow the caller's scope, never widen it: see Caller.scope.
    property_id = caller.scope(body.get("propertyId"))

    operating_date = (body.get("operatingDate") or "").strip()
    if operating_date:
        _validate_date(operating_date)

    run_id = str(uuid.uuid4())
    payload = {
        "prompt": prompt,
        "trigger": "chat",
        "runId": run_id,
        # The operator's real groups, from the token. The orchestrator surfaces
        # these to the sub-agents so a copilot run inherits the human's authority
        # rather than the agent identity's.
        "callerGroups": list(caller.groups),
    }
    if property_id:
        payload["propertyId"] = property_id
    if operating_date:
        payload["operatingDate"] = operating_date

    _sqs.send_message(QueueUrl=CHAT_QUEUE_URL, MessageBody=json.dumps(payload))
    _record_queued(payload, caller)

    return {
        "runId": run_id,
        "propertyId": property_id,
        "trigger": "chat",
        "askedBy": caller.email,
        "queuedAt": datetime.now(timezone.utc).isoformat(),
        # Told explicitly rather than left for the client to guess, so a future
        # console does not invent its own polling interval per screen.
        "poll": {
            "url": f"/api/runs/{run_id}",
            "intervalSeconds": 2,
            "note": (
                "Rows appear as the agents work: one per tool call, then a summary "
                "carrying the answer. status becomes 'complete' when the summary "
                "lands."
            ),
        },
    }


def _record_queued(payload: dict, caller) -> None:
    """Write a `queued` row before anything else can.

    Without this, ``GET /runs/{runId}`` has nothing to return until the invoker picks
    the message up and the first tool call lands -- so the console's poll got a 404
    for the first several seconds of every single run. 450 of them in one day, plus
    throttling on the retries. A 404 on the happy path is not a client problem to
    handle; it is a missing row.

    Two things fall out of fixing it here rather than in the browser. `404` starts
    meaning "no such run", which is what a reader assumes it means. And a run that is
    enqueued but never executes -- a message that dies in the DLQ -- becomes *visible*
    in run history as permanently queued, where before it left no trace at all.

    Best-effort: a failure here must not lose a run that is already on the queue.
    """
    now = datetime.now(timezone.utc)
    try:
        table("DECISIONS_TABLE").put_item(
            Item={
                "run_id": payload["runId"],
                # Sorts before every tool call and before the invoker's `zz-summary`,
                # so a Query returns queued -> trajectory -> conclusion in order.
                "event_id": f"{now.isoformat()}#00-queued",
                "ts": now.isoformat(),
                "property_id": payload.get("propertyId") or "_chain",
                "agent": "console",
                "kind": "run_queued",
                "trigger": payload["trigger"],
                "operating_date": payload.get("operatingDate") or "",
                "prompt": payload["prompt"],
                "asked_by": caller.email or caller.subject,
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record the queued run %s", payload["runId"])


def _validate_date(value: str) -> None:
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(
            400, "INVALID_DATE", f"operatingDate must be YYYY-MM-DD, got {value!r}."
        ) from exc


handler = handler_for(route)
