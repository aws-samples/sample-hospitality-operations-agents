#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase 5 verification: are the graders actually grading?

An evaluation stack that deploys cleanly and silently scores nothing is the worst
possible outcome, because it produces exactly the reassurance it was built to
replace. So this checks the two things that can each be true or false independently:

1. **The configuration is live and points at the right traffic** -- ACTIVE, ENABLED,
   the Runtime's real log group and OTEL service name, and all six evaluators
   attached. Cheap, no model involved.
2. **Scores exist, for real sessions, with reasons attached.** This reads the
   dedicated results log group the service writes to and reports what it found,
   per evaluator. If a grader returned nothing, that grader is named.

The second check needs a graded session to exist. Sampling is a percentage, so on a
10% config most runs are simply not graded and finding nothing means nothing. Deploy
with ``-c evaluationSampling=100`` and run one agent before trusting a negative
result here -- the script says so rather than reporting a false failure.

Usage::

    AWS_PROFILE=... tests/integration/verify_evaluation.py [--since-minutes 60]
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

import boto3

REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")
STACK = "hotel-ops-agent-evaluation"
AGENTCORE_STACK = "hotel-ops-agent-agentcore"

#: How far back to look for scores. Evaluation is asynchronous -- the judges run
#: after a session closes, and a session closes after the run does -- so a window
#: measured in minutes rather than seconds is the honest default.
DEFAULT_WINDOW_MINUTES = 90

#: How long to wait for a score to turn up before calling it absent.
SCORE_TIMEOUT_SECONDS = 600
POLL_SECONDS = 20

cfn = boto3.client("cloudformation", region_name=REGION)
control = boto3.client("bedrock-agentcore-control", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)

results: list[tuple[bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return ok


def outputs(stack: str) -> dict[str, str]:
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    }


# --------------------------------------------------------------------------- #
# 1. The configuration
# --------------------------------------------------------------------------- #


def verify_config(out: dict[str, str], agentcore_out: dict[str, str]) -> dict:
    print("=" * 72)
    print("Phase 5: online evaluation")
    print("=" * 72)

    config = control.get_online_evaluation_config(
        onlineEvaluationConfigId=out["OnlineEvaluationConfigId"]
    )

    check(
        "the online evaluation config is ACTIVE and ENABLED",
        config.get("status") == "ACTIVE" and config.get("executionStatus") == "ENABLED",
        f"status={config.get('status')} execution={config.get('executionStatus')}",
    )

    runtime_id = agentcore_out["RuntimeArn"].rsplit("/", 1)[-1]
    expected_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-production"
    source = (config.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}
    check(
        "it reads the deployed Runtime's own log group, resolved from the construct "
        "rather than hardcoded",
        expected_group in (source.get("logGroupNames") or []),
        f"logGroupNames={source.get('logGroupNames')}",
    )
    check(
        "and filters on the OTEL service name the Runtime actually reports",
        "hotel_ops_agent.production" in (source.get("serviceNames") or []),
        f"serviceNames={source.get('serviceNames')}",
    )

    attached = {e["evaluatorId"] for e in config.get("evaluators") or []}
    expected_builtin = set(out["BuiltinEvaluators"].split(","))
    check(
        "the four built-in trajectory graders are attached",
        expected_builtin <= attached,
        f"missing={sorted(expected_builtin - attached) or 'none'}",
    )
    check(
        "the session-level honesty judge is on the shared config",
        out["HonestyEvaluatorId"] in attached,
        ", ".join(sorted(attached - expected_builtin)),
    )
    check(
        "and the room judge is NOT -- it has its own config, because a TOOL_CALL "
        "evaluator cannot be scoped to one tool within a config",
        out["RoomQualityEvaluatorId"] not in attached,
        "it would otherwise grade every read in every sampled session",
    )

    room = control.get_online_evaluation_config(
        onlineEvaluationConfigId=out["RoomQualityConfigId"]
    )
    room_attached = {e["evaluatorId"] for e in room.get("evaluators") or []}
    filters = (room.get("rule") or {}).get("filters") or []
    check(
        "the room-quality config carries the room judge and a filter scoping it to "
        "assignments",
        room_attached == {out["RoomQualityEvaluatorId"]} and bool(filters),
        f"evaluators={sorted(room_attached)} filters={json.dumps(filters)}",
    )

    sampling = (config.get("rule") or {}).get("samplingConfig", {}).get(
        "samplingPercentage"
    )
    print(f"\n        sampling: {sampling}%")
    if sampling and sampling < 100:
        print(
            "        note: below 100%, most sessions are not graded. A missing score "
            "below\n              is not evidence of a broken grader."
        )

    # The judges each invoke a model, and the construct's auto-created role does not
    # grant that -- it has no way to know which model a custom evaluator uses. A
    # missing grant produces scores that never appear, with the failure buried in a
    # service-side log nobody owns.
    for evaluator_id, name in (
        (out["RoomQualityEvaluatorId"], "room quality"),
        (out["HonestyEvaluatorId"], "answer honesty"),
    ):
        evaluator = control.get_evaluator(evaluatorId=evaluator_id)
        judge = (
            (evaluator.get("evaluatorConfig") or {})
            .get("llmAsAJudge", {})
            .get("modelConfig", {})
            .get("bedrockEvaluatorModelConfig", {})
        )
        instructions = (
            (evaluator.get("evaluatorConfig") or {})
            .get("llmAsAJudge", {})
            .get("instructions", "")
        )
        check(
            f"the {name} judge is configured with a model and a template that "
            "interpolates the session",
            bool(judge.get("modelId")) and "{" in instructions,
            f"model={judge.get('modelId')} level={evaluator.get('level')} "
            f"placeholders={sorted(set(_placeholders(instructions)))}",
        )

    return config


def _placeholders(text: str) -> list[str]:
    import re

    return re.findall(r"\{(\w+)\}", text)


# --------------------------------------------------------------------------- #
# 2. The scores
# --------------------------------------------------------------------------- #


def verify_scores(configs: list[dict], window_minutes: int, wait: bool) -> None:
    print("\n  -- the scores themselves --\n")

    groups = [
        g
        for g in (
            (c.get("outputConfig") or {}).get("cloudWatchConfig", {}).get("logGroupName")
            for c in configs
        )
        if g
    ]
    if not groups:
        check(
            "the service published a results destination",
            False,
            "no outputConfig.cloudWatchConfig.logGroupName on either config",
        )
        return
    for g in groups:
        print(f"        results log group: {g}")

    deadline = time.time() + (SCORE_TIMEOUT_SECONDS if wait else 0)
    records: list[dict] = []
    while True:
        records = [r for g in groups for r in _read_results(g, window_minutes)]
        if records or time.time() >= deadline:
            break
        print(f"        no scores yet; waiting {POLL_SECONDS}s")
        time.sleep(POLL_SECONDS)

    if not check(
        "the graders have written scores for real sessions",
        bool(records),
        f"{len(records)} score records in the last {window_minutes} minutes"
        + (
            ""
            if records
            else " -- if sampling is below 100% this is expected; if it is 100% and a "
            "session has run, check the execution role's bedrock:InvokeModel grant"
        ),
    ):
        return

    by_evaluator: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_evaluator[_evaluator_of(record)].append(record)

    print()
    for evaluator, scored in sorted(by_evaluator.items()):
        values = [v for v in (_score_of(r) for r in scored) if v is not None]
        mean = f"{sum(values) / len(values):.2f}" if values else "n/a"
        sessions = {_attributes(r).get(SESSION_KEY) for r in scored}
        print(
            f"        {evaluator:40} n={len(scored):3} mean={mean:>5} "
            f"range={min(values) if values else '-'}..{max(values) if values else '-'} "
            f"sessions={len(sessions)}"
        )

    # The limitation the split configs exist to contain. A judge that says its own
    # subject does not apply is grading something nobody asked about, and the mean it
    # lands in is not a quality signal. Reported rather than hidden, because the filter
    # key scoping the room judge is unverified.
    inapplicable = [
        r
        for r in records
        if "room_assignment" in _evaluator_of(r)
        and "not a room-assignment" in (_explanation_of(r) or "")
    ]
    check(
        "the room-assignment judge is not being run against unrelated tool calls "
        f"(within the last {window_minutes} minutes)",
        not inapplicable,
        f"{len(inapplicable)} of "
        f"{len([r for r in records if 'room_assignment' in _evaluator_of(r)])} "
        "room-quality gradings say the call was not an assignment"
        + (
            " -- the filter on the room-quality config is not matching; its mean is "
            "diluted by reads and should not be read as room quality yet"
            if inapplicable
            else ""
        ),
    )

    check(
        "more than one evaluator produced a score, so this is not one grader working "
        "and five silently failing",
        len(by_evaluator) > 1,
        f"{len(by_evaluator)} evaluators reported: {sorted(by_evaluator)}",
    )

    reasoned = [r for r in records if _explanation_of(r)]
    check(
        "scores carry the judge's reasoning, not just a number -- a grade nobody can "
        "argue with is a grade nobody can improve",
        bool(reasoned),
        f"{len(reasoned)}/{len(records)} records include an explanation",
    )

    # The lowest score with a reason: the one an operator would actually want to
    # read, and the fastest way to tell a working rubric from a rubber stamp.
    scored = [r for r in records if _score_of(r) is not None and _explanation_of(r)]
    if scored:
        worst = min(scored, key=lambda r: _score_of(r))
        print(
            f"\n        lowest-scoring judgement -- {_evaluator_of(worst)} "
            f"= {_score_of(worst)}:"
        )
        print("        " + " ".join((_explanation_of(worst) or "").split())[:700])


def _read_results(group: str, window_minutes: int) -> list[dict]:
    """Every score record in the window.

    Paginated, because ``filter_log_events`` returns empty pages with a nextToken
    when a scanned chunk holds no match -- a single call reports nothing while the
    records sit one page further on. That mistake cost four false negatives earlier in
    this project; it is not repeated here.
    """
    start = int(time.time() - window_minutes * 60) * 1000
    records: list[dict] = []
    try:
        pages = logs.get_paginator("filter_log_events").paginate(
            logGroupName=group, startTime=start
        )
        for page in pages:
            for event in page["events"]:
                try:
                    record = json.loads(event["message"])
                except ValueError:
                    continue
                # The group also carries non-score records; only results count.
                if _is_score(record):
                    records.append(record)
    except logs.exceptions.ResourceNotFoundException:
        # The service creates the group on the first result, so "not found" and "no
        # scores yet" are the same state.
        return []
    return records


# A score record is an OTEL log record named `gen_ai.evaluation.result`, with
# everything of interest under `attributes` using the GenAI convention keys below.
# Read off a live result rather than guessed: an earlier version of this file looked
# for top-level "score"/"explanation" keys and would have reported every real score as
# unparseable.
EVALUATOR_KEY = "gen_ai.evaluation.name"
SCORE_KEY = "gen_ai.evaluation.score.value"
EXPLANATION_KEY = "gen_ai.evaluation.explanation"
SESSION_KEY = "session.id"


def _attributes(record: dict) -> dict:
    return record.get("attributes") or {}


def _is_score(record: dict) -> bool:
    return EVALUATOR_KEY in _attributes(record)


def _evaluator_of(record: dict) -> str:
    return str(_attributes(record).get(EVALUATOR_KEY, "(unidentified)"))


def _score_of(record: dict):
    value = _attributes(record).get(SCORE_KEY)
    return value if isinstance(value, (int, float)) else None


def _explanation_of(record: dict) -> str | None:
    value = _attributes(record).get(EXPLANATION_KEY)
    return value if isinstance(value, str) and value.strip() else None


# --------------------------------------------------------------------------- #


def main() -> int:
    window = DEFAULT_WINDOW_MINUTES
    if "--since-minutes" in sys.argv:
        window = int(sys.argv[sys.argv.index("--since-minutes") + 1])
    wait = "--no-wait" not in sys.argv

    out = outputs(STACK)
    config = verify_config(out, outputs(AGENTCORE_STACK))
    room = control.get_online_evaluation_config(
        onlineEvaluationConfigId=out["RoomQualityConfigId"]
    )
    verify_scores([config, room], window, wait)

    failed = [label for ok, label in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
