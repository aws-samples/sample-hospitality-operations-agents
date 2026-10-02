#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run every cadence agent once through the real unattended path.

Layer 3 proved A1 by invoking the Runtime directly, and Layer 5 proved A4 through
the queue. A2, A3 and A5 had never executed at all -- their tool Lambdas passed
Layer 1 and their tools were discoverable in Layer 2, but no model had ever driven
them. Both bugs found so far (``list_arrivals`` returning the wrong set, and the
orchestrator never receiving the run context) surfaced the first time an agent
actually ran, so the four that had not run were four untested assumptions.

Each agent is invoked by enqueuing the payload from **its own deployed schedule**,
so what executes is what a cadence would send, and the run travels the whole path:
SQS -> invoker -> Runtime -> orchestrator -> sub-agent -> Gateway -> interceptors
-> tool Lambda -> Cognito -> foundation.

This writes to the foundation. A1 pre-assigns rooms and A2 assigns and sequences
housekeeping tasks -- both Tier 1, auto-execute by design, and reversible by a
human at the desk. A3 is Tier 2 and cannot write without an approval token; A5 has
no write endpoint to reach. Pass ``--only a4,a5`` to restrict the set to
non-writing agents.

Usage::

    AWS_PROFILE=... tests/integration/exercise_agents.py [--only a1,a2,...]
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import boto3

STACK = "hotel-ops-agent-orchestration"
REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")
GROUP = "hotel-ops-agent"

#: Schedule-name prefix -> the sub-agent the orchestrator is expected to pick. A3
#: has no schedule of its own (it is reactive only), so it is driven by the prompt
#: its checkout rule sends.
AGENTS = {
    "a1": ("hotel-ops-a1-arrivals", "arrivals_agent"),
    "a2": ("hotel-ops-a2-housekeeping", "housekeeping_agent"),
    "a4": ("hotel-ops-a4-nightaudit", "night_audit_agent"),
    "a5": ("hotel-ops-a5-regional", "regional_agent"),
}

#: How long to wait for the slowest run. These execute concurrently, bounded by the
#: invoker's reserved concurrency of 5.
TIMEOUT_SECONDS = 900
POLL_SECONDS = 15

cfn = boto3.client("cloudformation", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
scheduler = boto3.client("scheduler", region_name=REGION)

results: list[tuple[bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return ok


def main() -> int:
    only = _requested()
    out = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    }
    schedules = scheduler.list_schedules(GroupName=GROUP)["Schedules"]

    print("=" * 72)
    print(f"Exercising {', '.join(sorted(only))} through the unattended path")
    print("=" * 72)

    queued: dict[str, tuple[str, dict]] = {}
    for key in sorted(only):
        prefix, expected_agent = AGENTS[key]
        match = next((s for s in schedules if s["Name"].startswith(prefix)), None)
        if match is None:
            check(f"{key}: a deployed schedule exists", False, f"no schedule {prefix}*")
            continue
        payload = json.loads(
            scheduler.get_schedule(GroupName=GROUP, Name=match["Name"])["Target"]["Input"]
        )
        run_id = str(uuid.uuid4())
        payload["runId"] = run_id
        sqs.send_message(
            QueueUrl=out["InvocationQueueUrl"], MessageBody=json.dumps(payload)
        )
        queued[key] = (run_id, payload)
        print(f"  queued {key} ({match['Name']}) as run {run_id}")

    print(f"\nWaiting up to {TIMEOUT_SECONDS}s for {len(queued)} concurrent runs...\n")

    finished: dict[str, list[dict]] = {}
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline and len(finished) < len(queued):
        for key, (run_id, _) in queued.items():
            if key in finished:
                continue
            rows = _rows(out["DecisionsTableName"], run_id)
            if any(r.get("kind", {}).get("S") == "run_summary" for r in rows):
                finished[key] = rows
                print(f"  {key} finished ({len(rows)} rows)")
        if len(finished) < len(queued):
            time.sleep(POLL_SECONDS)

    print()
    for key, (run_id, payload) in sorted(queued.items()):
        _report(key, run_id, payload, finished.get(key))

    failed = [label for ok, label in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


def _requested() -> set[str]:
    if "--only" in sys.argv:
        raw = sys.argv[sys.argv.index("--only") + 1]
        requested = {k.strip().lower() for k in raw.split(",") if k.strip()}
        unknown = requested - set(AGENTS)
        if unknown:
            raise SystemExit(f"unknown agent(s) {sorted(unknown)}; known: {sorted(AGENTS)}")
        return requested
    return set(AGENTS)


def _code_interpreter_session(summary: dict) -> str | None:
    """The sandbox A5 opened, from the Runtime's own log group.

    Correlated by *time window*, not by run id or trace. Neither is available: the
    sandbox is a direct data-plane call with no run id in scope, and ``code_execution``
    logs the memory session name (``a5-{property}-{date}``), which is deliberately
    shared across runs on the same day so a warm container reconnects instead of
    paying to build a sandbox. So this asks whether a session was started while this
    run was executing, which is sound because this script runs one A5 at a time.

    Three earlier versions of this check were wrong, every one of them reporting a
    *working* Code Interpreter as unused. A false negative in a verification script
    is the expensive kind of bug -- it sends you fixing something that was never
    broken -- so all three are recorded here:

    * The first looked for the call in the decision log. It cannot be there: the
      sandbox never touches the Gateway, so no interceptor ever sees it.
    * The second filtered on ``'"a" "b"'``, which CloudWatch does not read as AND.
      It matched nothing and found no traces to correlate against.
    * The third called ``filter_log_events`` once. CloudWatch returns *empty pages
      with a nextToken* when a scanned chunk holds no match, so a single call finds
      nothing while the events sit one page further on. Hence the paginator below --
      and it is why this function asserts a negative only after exhausting the
      window.
    """
    logs = boto3.client("logs", region_name=REGION)
    # Derived from the deployed Runtime's ARN, not by prefix-matching. This account
    # holds more than one `hotel_ops_agent-*-production` log group -- an earlier
    # runtime's is still there -- and taking the first match read a group that has
    # not been written to for hours. That was the fourth false negative here.
    runtime_arn = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName="hotel-ops-agent-agentcore")["Stacks"][0][
            "Outputs"
        ]
    }["RuntimeArn"]
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-production"

    from datetime import datetime

    started = datetime.fromisoformat(summary["started_at"]["S"])
    duration = float(summary.get("duration_seconds", {}).get("N", "0"))
    # A minute either side: the log timestamp is when the line was written, and the
    # summary is stamped after the stream closes.
    window_start = int(started.timestamp() - 60) * 1000
    window_end = int(started.timestamp() + duration + 60) * 1000

    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=group,
        startTime=window_start,
        endTime=window_end,
        filterPattern='"code interpreter session="',
    )
    for page in pages:
        for event in page["events"]:
            body = event["message"]
            marker = body.find("code interpreter session=")
            if marker >= 0:
                return body[marker : marker + 120].split('"')[0].strip()
    return None


def _rows(table: str, run_id: str) -> list[dict]:
    return ddb.query(
        TableName=table,
        KeyConditionExpression="run_id = :r",
        ExpressionAttributeValues={":r": {"S": run_id}},
        ConsistentRead=True,
    )["Items"]


def _report(key: str, run_id: str, payload: dict, rows: list[dict] | None) -> None:
    _, expected_agent = AGENTS[key]
    print(f"-- {key.upper()}  run {run_id}")

    if not rows:
        check(
            f"{key}: the run completed and filed a summary",
            False,
            "no summary row -- check /aws/lambda/hotel-ops-agent-invoker and the DLQ",
        )
        return

    summary = next(r for r in rows if r.get("kind", {}).get("S") == "run_summary")
    tools = [r for r in rows if r.get("kind", {}).get("S") != "run_summary"]
    delegated = [d["S"] for d in summary.get("delegations", {}).get("L", [])]
    writes = sorted(
        {
            f"{r.get('agent', {}).get('S')}.{r['action_taken']['S']}"
            for r in tools
            if "action_taken" in r
        }
    )

    check(
        f"{key}: the run succeeded",
        summary.get("outcome", {}).get("S") == "ok",
        f"outcome={summary.get('outcome', {}).get('S')} "
        f"error={summary.get('error_code', {}).get('S', '-')} "
        f"duration={summary.get('duration_seconds', {}).get('N')}s",
    )
    check(
        f"{key}: the orchestrator delegated to {expected_agent}",
        expected_agent in delegated,
        f"delegations={delegated or 'nothing'}",
    )
    check(
        f"{key}: the specialist actually used its tools",
        len(tools) > 0,
        f"{len(tools)} tool calls: "
        + ", ".join(sorted({r.get("tool", {}).get("S", "?") for r in tools})),
    )
    check(
        f"{key}: no tool call was refused for want of authority it should have",
        not any(
            r.get("error_code", {}).get("S", "").startswith("Forbidden")
            or r.get("error_code", {}).get("S") == "FORBIDDEN"
            for r in tools
        ),
        ", ".join(
            sorted(
                {
                    r["error_code"]["S"]
                    for r in tools
                    if "error_code" in r
                }
            )
        )
        or "no tool errors",
    )

    if key == "a5":
        # A5 is the only agent given a Code Interpreter, and the plan's
        # justification was that variance and trend math over 92 days of JSON is
        # what an LLM does worst and most confidently.
        #
        # Checked in the Runtime's log group, not the decision log: the sandbox is
        # a direct data-plane call, not a Gateway tool, so no interceptor ever sees
        # it and no row can exist. A first version of this check looked for it in
        # the decision log and reported a working Code Interpreter as unused.
        session = _code_interpreter_session(summary)
        check(
            "a5: the Code Interpreter was used for the arithmetic, not the model",
            session is not None,
            session or "no 'code interpreter session=' line in the Runtime log",
        )
    if key in ("a4", "a5"):
        check(
            f"{key}: an advisory agent wrote nothing",
            not writes,
            f"action_taken rows: {writes or 'none'}",
        )
    if key in ("a1", "a2"):
        # Tier 1 auto-execute. Zero writes is a legitimate outcome -- there may be
        # nothing to do -- so this reports rather than asserts.
        print(f"        writes: {writes or 'none (may be nothing to do)'}")

    answer = " ".join(summary.get("recommendation", {}).get("S", "").split())
    print(f"        answer: {answer[:400]}\n")


if __name__ == "__main__":
    sys.exit(main())
