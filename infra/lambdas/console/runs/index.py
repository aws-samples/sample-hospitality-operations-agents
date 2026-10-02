# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run history: the decision log, read back for humans.

Three routes:

* ``GET /runs`` -- recent runs, newest first, filtered by property. Reads the
  ``property_id-ts-index`` GSI, because the base table is partitioned by ``run_id``
  and "everything that happened at this hotel today" would otherwise be a scan.
* ``GET /runs/{runId}`` -- one run in full: every tool call in the order it
  happened, then the summary. This is also the chat poll target, so it reports a
  ``status`` and works perfectly well on a run that is still going.
* ``POST /runs/{runId}/override`` -- record that a human disagreed.

Why the override matters more than it looks
------------------------------------------
``PATTERN_EXTENSION_GUIDE.md`` §3.4 asks whether the agents are actually *right*, and no
amount of tracing answers that: the answer arrives days later when a person either
lets a decision stand or reverses it. ``human_override`` is where that judgment
lands, and it is the ground-truth signal Phase 5's evaluators score against. So it
is stored with who said it and why, not as a bare boolean -- "a human disagreed" is
a weak signal; "a human disagreed because the guest had asked for a low floor" is a
training example.

It is deliberately writable by any authenticated operator, including FrontDesk and
Housekeeping. Recording that the agent got something wrong is not a privileged act,
and requiring a manager for it would systematically under-collect exactly the
signal the system most needs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from hotel_console.api import (
    ApiError,
    body_of,
    handler_for,
    int_param,
    path_param,
    query_param,
    table,
)

#: A page of run history. Generous because a row is small and an operator scrolling
#: back through a shift should not paginate every ten rows.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Written by ``agent_invoker`` on the summary row. Kept in sync by name only, like
#: the table names themselves -- there is no shared module between a Runtime-side
#: Lambda and this one.
SUMMARY_KIND = "run_summary"

#: Written by the chat route the moment a run is enqueued, so `GET /runs/{runId}`
#: has something truthful to return before the invoker has even picked it up.
QUEUED_KIND = "run_queued"

MAX_OVERRIDE_REASON = 2000

#: Minimum rows read from the index per query, whatever the caller's limit. Sized
#: against the busiest observed run: A5's portfolio review wrote 110 tool rows, so a
#: few hundred covers several such runs without paging.
MIN_OVER_READ = 400


def route(event: dict, caller):
    method = event.get("httpMethod")
    path = event.get("resource") or ""

    if method == "GET" and path.endswith("/runs"):
        return _list_runs(event, caller)
    if method == "GET":
        return _get_run(event, caller)
    if method == "POST":
        return _override(event, caller)
    raise ApiError(405, "METHOD_NOT_ALLOWED", f"{method} {path} is not a route here.")


# --------------------------------------------------------------------------- #
# GET /runs
# --------------------------------------------------------------------------- #


def _list_runs(event: dict, caller) -> dict:
    """Recent run summaries for one property, or for the chain.

    Only summaries: a list view wants one row per run, and the tool calls belong to
    the detail view. The GSI carries every row, so the filter happens here -- which
    reads more rows than it returns, and is still far cheaper than the scan the base
    table would need.
    """
    property_id = caller.scope(query_param(event, "propertyId"))
    limit = int_param(event, "limit", DEFAULT_LIMIT, maximum=MAX_LIMIT)

    # Chain-wide runs (A5) are stored under "_chain" by the invoker, because a GSI
    # partition key cannot be null. A chain-level operator asking for everything
    # gets both their properties' runs and the chain's own.
    partitions = [property_id] if property_id else ["_chain"]
    if property_id is None:
        requested = query_param(event, "includeProperties")
        if requested:
            partitions.extend(p.strip() for p in requested.split(",") if p.strip())

    runs: list[dict] = []
    decisions = table("DECISIONS_TABLE")
    for partition in partitions:
        response = decisions.query(
            IndexName="property_id-ts-index",
            KeyConditionExpression=Key("property_id").eq(partition),
            # Summaries *and* still-queued runs. A run that never executed is the
            # one an operator most needs to see, and filtering to summaries alone
            # hid exactly that case.
            FilterExpression="#k IN (:summary, :queued)",
            ExpressionAttributeNames={"#k": "kind"},
            ExpressionAttributeValues={
                ":summary": SUMMARY_KIND,
                ":queued": QUEUED_KIND,
            },
            ScanIndexForward=False,
            # Over-read, because DynamoDB applies Limit *before* the filter: a page
            # of summaries sits behind however many tool rows those runs made.
            #
            # A floor, not just a multiple. With `Limit=limit * 12` a request for one
            # run read twelve rows and found nothing, because a single A5 portfolio
            # review writes over a hundred tool rows -- so the newest twelve rows in
            # the partition were all tool calls and the endpoint reported no runs at
            # all. Scaling the over-read by the caller's limit gets the relationship
            # backwards: the rows to skip are a property of the runs, not of how many
            # the caller asked for.
            Limit=max(limit * 12, MIN_OVER_READ),
        )
        runs.extend(_summarize(item) for item in response.get("Items", []))

    # Collapse to one row per run, preferring the summary.
    #
    # A completed run has *both* a `run_queued` row (written by the chat route at
    # enqueue time) and a `run_summary` row, so returning the raw query listed every
    # chat run twice -- once correctly, and once badged "queued / no delegation" as
    # though it had never executed. A `queued` row is only news when no summary exists
    # for the same run, which is exactly the DLQ case it was added to surface.
    latest: dict[str, dict] = {}
    for run in runs:
        existing = latest.get(run["runId"])
        if existing is None or (
            existing["status"] == "queued" and run["status"] != "queued"
        ):
            latest[run["runId"]] = run

    runs = sorted(latest.values(), key=lambda r: r["ts"], reverse=True)
    return {
        "runs": runs[:limit],
        "propertyId": property_id,
        "count": len(runs[:limit]),
        "note": (
            "Summaries only. GET /runs/{runId} returns the tool calls the run made."
        ),
    }


def _summarize(item: dict) -> dict:
    """The list-view projection. Deliberately not the whole row.

    The recommendation is trimmed here: a portfolio review runs to several thousand
    characters, and fifty of them would make the list response megabytes for text
    nobody reads until they open one.
    """
    recommendation = item.get("recommendation") or ""
    return {
        "status": "queued" if item.get("kind") == QUEUED_KIND else "complete",
        "runId": item.get("run_id"),
        "ts": item.get("ts"),
        "propertyId": item.get("property_id"),
        "operatingDate": item.get("operating_date"),
        "agent": item.get("agent"),
        "trigger": item.get("trigger"),
        "outcome": item.get("outcome"),
        "delegations": item.get("delegations") or [],
        "durationSeconds": item.get("duration_seconds"),
        "prompt": item.get("prompt"),
        "excerpt": recommendation[:400],
        "truncated": len(recommendation) > 400,
        "humanOverride": item.get("human_override"),
        "errorCode": item.get("error_code"),
    }


# --------------------------------------------------------------------------- #
# GET /runs/{runId}
# --------------------------------------------------------------------------- #


def _get_run(event: dict, caller) -> dict:
    run_id = path_param(event, "runId")
    items = _rows(run_id)
    if not items:
        raise ApiError(
            404,
            "RUN_NOT_FOUND",
            f"No run {run_id}. A run that was only just queued may not have written "
            "its first row yet; keep polling.",
        )

    summary = next((i for i in items if i.get("kind") == SUMMARY_KIND), None)
    queued = next((i for i in items if i.get("kind") == QUEUED_KIND), None)
    tool_rows = [
        i for i in items if i.get("kind") not in (SUMMARY_KIND, QUEUED_KIND)
    ]

    # Scope check against the rows, not the request: a run's property comes from the
    # correlation headers the agent sent, so this is the authoritative answer to
    # "whose run is this".
    _assert_readable(caller, summary or queued or (tool_rows[0] if tool_rows else {}))

    return {
        "runId": run_id,
        # The whole reason this endpoint doubles as the chat poll target. Three
        # states, not two: a run sits `queued` until the invoker picks it up, and
        # reporting that honestly is what stopped the console polling into a 404.
        "status": (
            "complete" if summary else "running" if tool_rows else "queued"
        ),
        "prompt": (summary or queued or {}).get("prompt"),
        "askedBy": (queued or {}).get("asked_by"),
        "summary": _summarize(summary) if summary else None,
        "answer": (summary or {}).get("recommendation"),
        "answerTruncated": bool((summary or {}).get("recommendation_truncated")),
        "usage": (summary or {}).get("usage"),
        "steps": [
            {
                "ts": row.get("ts"),
                "agent": row.get("agent"),
                "tool": row.get("tool"),
                "outcome": row.get("outcome"),
                "errorCode": row.get("error_code"),
                # The action, when one was taken. Present only on a *successful*
                # write, so this column never implies money moved that did not.
                "actionTaken": row.get("action_taken"),
                # The hash, never the arguments: they carry guest names, folio
                # detail, and on a Tier-2 call the approval token itself.
                "inputsHash": row.get("inputs_hash"),
            }
            # Sorted by the sort key, which is `{iso}#{suffix}` -- chronological by
            # construction, so this is the order the agent actually worked in.
            for row in sorted(tool_rows, key=lambda r: r.get("event_id", ""))
        ],
        "stepCount": len(tool_rows),
        "humanOverride": (summary or {}).get("human_override"),
    }


def _rows(run_id: str) -> list[dict]:
    decisions = table("DECISIONS_TABLE")
    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": Key("run_id").eq(run_id),
        # A run being polled is being written to concurrently; an eventually
        # consistent read would show a stale, shorter trajectory and make the run
        # look stalled.
        "ConsistentRead": True,
    }
    while True:
        response = decisions.query(**kwargs)
        items.extend(response.get("Items", []))
        token = response.get("LastEvaluatedKey")
        if not token:
            return items
        kwargs["ExclusiveStartKey"] = token


def _assert_readable(caller, row: dict) -> None:
    property_id = row.get("property_id")
    if property_id in (None, "", "_chain", "_unknown"):
        # A chain-wide or unattributed run. Chain-level operators only: a caller
        # pinned to one property has no business reading a portfolio-wide run.
        if caller.property_id:
            raise ApiError(
                403,
                "OUT_OF_SCOPE",
                "This run is chain-wide and your account is scoped to one property.",
            )
        return
    caller.scope(property_id)


# --------------------------------------------------------------------------- #
# POST /runs/{runId}/override
# --------------------------------------------------------------------------- #


def _override(event: dict, caller) -> dict:
    run_id = path_param(event, "runId")
    body = body_of(event)

    reason = (body.get("reason") or "").strip()
    if not reason:
        raise ApiError(
            400,
            "MISSING_REASON",
            "An override needs a reason. The bare fact that someone disagreed is a "
            "weak signal; why they disagreed is what the evaluators can learn from.",
        )
    if len(reason) > MAX_OVERRIDE_REASON:
        raise ApiError(
            400, "REASON_TOO_LONG", f"Limit {MAX_OVERRIDE_REASON} characters."
        )

    verdict = (body.get("verdict") or "overridden").strip().lower()
    if verdict not in ("overridden", "endorsed"):
        raise ApiError(
            400,
            "INVALID_VERDICT",
            "verdict must be 'overridden' (a human reversed or corrected this) or "
            "'endorsed' (a human reviewed it and let it stand). Both are signal; "
            "only recording the failures would teach the evaluator that every "
            "reviewed decision was wrong.",
        )

    items = _rows(run_id)
    summary = next((i for i in items if i.get("kind") == SUMMARY_KIND), None)
    if summary is None:
        raise ApiError(
            404,
            "RUN_NOT_SUMMARIZED",
            f"Run {run_id} has no summary row yet, so there is no decision to "
            "override. Wait for it to finish.",
        )
    _assert_readable(caller, summary)

    recorded = {
        "verdict": verdict,
        "reason": reason,
        # From the token, never the body. An override attributed to someone who did
        # not make it is worse than no override at all.
        "by": caller.email or caller.subject,
        "bySubject": caller.subject,
        "at": datetime.now(timezone.utc).isoformat(),
    }

    table("DECISIONS_TABLE").update_item(
        Key={"run_id": run_id, "event_id": summary["event_id"]},
        UpdateExpression="SET human_override = :o",
        ExpressionAttributeValues={":o": recorded},
        # Written on the summary row, so one Query returns the run and the verdict
        # on it together. No condition expression: a second reviewer changing their
        # colleague's verdict is a legitimate act, and the previous one is
        # recoverable from the table's point-in-time recovery.
    )

    return {"runId": run_id, "humanOverride": recorded}


handler = handler_for(route)
