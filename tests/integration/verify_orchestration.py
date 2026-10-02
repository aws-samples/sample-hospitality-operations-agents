#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Layer 5 verification: unattended operation, against the deployed stack.

Four things, in the order that a failure would matter most:

1. **The two tables exist in the shape the already-deployed interceptors expect.**
   Both were referenced by *name* from ``tools_stack`` and ``agentcore_stack``, so
   nothing has ever type-checked them against each other. A key schema or TTL
   attribute that disagrees produces no synth error, no deploy error, and a
   guardrail that silently cannot read its own approvals.
2. **The Tier-2 gate now verifies rather than merely refusing.** Before this stack
   the approvals table did not exist, so every billing write was refused as
   ``APPROVAL_UNVERIFIABLE`` -- fail-closed and correct, but also a gate that had
   never once been proven to *open*. This mints a real approval, presents it, and
   asserts the call reaches the foundation. The folio id is deliberately
   nonexistent, so "reached the foundation" is provable without moving money.
3. **A queued run executes end to end and lands in the decision log.** The payload
   used is read verbatim from a deployed schedule, so what is tested is what a
   cadence will actually send. Both writers are then asserted: the Gateway response
   interceptor's per-tool-call rows, which have never had a table to write to
   before now, and the invoker's run summary.
4. **The reactive rules match real foundation events.** Checked with
   ``TestEventPattern`` rather than by publishing, because the foundation's own
   consumers are subscribed to these same events -- injecting a synthetic checkout
   would create real housekeeping tasks and billing work for a stay that never
   happened. The pattern is the part that was worth doubting: the plan named
   ``crs.reservation_created`` and ``pms.checkout_completed``, and neither exists.

The prompt in check 3 is deliberately advise-only (A4), so a full verification run
writes nothing to the foundation.

Usage::

    AWS_PROFILE=... tests/integration/verify_orchestration.py [--no-model]
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3

STACK = "hotel-ops-agent-orchestration"
REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")

#: How long to wait for a queued agent run. Measured runs are 30-210s; the
#: invoker's own timeout is 15 minutes, so this is the shorter of the two bounds
#: and a timeout here means "slower than expected", not "broken".
RUN_TIMEOUT_SECONDS = 420
POLL_SECONDS = 10

cfn = boto3.client("cloudformation", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
scheduler = boto3.client("scheduler", region_name=REGION)
events = boto3.client("events", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

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
# 1. The tables, against what the deployed interceptors assume
# --------------------------------------------------------------------------- #


def verify_tables(out: dict[str, str]) -> None:
    print("=" * 72)
    print("Layer 5: unattended operation")
    print("=" * 72)

    decisions = ddb.describe_table(TableName=out["DecisionsTableName"])["Table"]
    keys = {k["KeyType"]: k["AttributeName"] for k in decisions["KeySchema"]}
    check(
        "the decision log is keyed run_id + event_id, which is what both writers "
        "use",
        keys == {"HASH": "run_id", "RANGE": "event_id"},
        json.dumps(keys),
    )

    indexes = {
        i["IndexName"]: {k["KeyType"]: k["AttributeName"] for k in i["KeySchema"]}
        for i in decisions.get("GlobalSecondaryIndexes") or []
    }
    check(
        "and carries the property_id + ts index the console's run history needs",
        indexes.get("property_id-ts-index")
        == {"HASH": "property_id", "RANGE": "ts"},
        json.dumps(indexes),
    )
    check(
        "the decision log survives a stack destroy, because it is the audit trail "
        "and the evaluation ground truth",
        _deletion_policy(STACK, "DecisionsTable") == "Retain",
        f"DeletionPolicy={_deletion_policy(STACK, 'DecisionsTable')}",
    )

    approvals = ddb.describe_table(TableName=out["ApprovalsTableName"])["Table"]
    approval_keys = {k["KeyType"]: k["AttributeName"] for k in approvals["KeySchema"]}
    check(
        "the approvals table is keyed approvalId, the token the interceptor looks up",
        approval_keys == {"HASH": "approvalId"},
        json.dumps(approval_keys),
    )

    ttl = ddb.describe_time_to_live(TableName=out["ApprovalsTableName"])[
        "TimeToLiveDescription"
    ]
    check(
        "TTL is on the expiresAt attribute the approval interceptor reads, so the "
        "two agree about what 'expired' means",
        ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING")
        and ttl.get("AttributeName") == "expiresAt",
        json.dumps(ttl),
    )

    interceptor = lambda_client.get_function_configuration(
        FunctionName="hotel-ops-agent-approval-interceptor"
    )
    env = interceptor.get("Environment", {}).get("Variables", {})
    check(
        "and the deployed interceptor is pointed at exactly this table, not at a "
        "name that drifted",
        env.get("APPROVALS_TABLE") == out["ApprovalsTableName"],
        f"interceptor APPROVALS_TABLE={env.get('APPROVALS_TABLE')!r}",
    )

    log_env = lambda_client.get_function_configuration(
        FunctionName="hotel-ops-agent-decision-log-interceptor"
    ).get("Environment", {}).get("Variables", {})
    check(
        "the decision-log interceptor likewise, so it stops discarding rows",
        log_env.get("DECISIONS_TABLE") == out["DecisionsTableName"],
        f"interceptor DECISIONS_TABLE={log_env.get('DECISIONS_TABLE')!r}",
    )


def _deletion_policy(stack: str, logical_prefix: str) -> str:
    template = cfn.get_template(StackName=stack, TemplateStage="Processed")[
        "TemplateBody"
    ]
    if isinstance(template, str):
        template = json.loads(template)
    for name, resource in (template.get("Resources") or {}).items():
        if name.startswith(logical_prefix):
            return resource.get("DeletionPolicy", "(unset)")
    return "(not found)"


# --------------------------------------------------------------------------- #
# 2. The gate, now that it can actually verify
# --------------------------------------------------------------------------- #

#: A folio that does not exist. The point of check 2 is to prove the approval was
#: honoured, which means proving the call reached the foundation -- and the
#: foundation's own 404 proves it without a cent moving.
NONEXISTENT_FOLIO = "00000000-0000-0000-0000-000000000000"


def verify_approval_gate(out: dict[str, str]) -> None:
    print("\n  -- the Tier-2 gate, now that the approvals table exists --\n")

    import importlib

    # Layer 2's loader, not a second copy of it. It reads the Gateway URL and the
    # memory namespaces from the deployed stack and -- critically -- pins
    # AWS_REGION to us-east-1 before the agent modules are imported. Without that
    # the SigV4 signer signs for the ambient region and the Gateway answers
    # "Authentication error - Invalid credentials", which reads like a broken
    # policy rather than a wrong region.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    importlib.import_module("verify_gateway").load_environment()

    sys.path.insert(0, os.path.join(_repo_root(), "agents"))
    gateway = importlib.import_module("gateway")
    run_context = importlib.import_module("run_context")
    run_context.set_current(
        run_context.RunContext(
            property_id=None,
            operating_date=datetime.now(timezone.utc).date().isoformat(),
            trigger="chat",
        )
    )

    table = out["ApprovalsTableName"]
    client = gateway._client_for("billing", gateway.correlation_headers("billing"))

    with client:
        # ---- a forged token, now that the table is reachable ---------------
        forged = client.call_tool_sync(
            tool_use_id="verify-orch-1",
            name="billing___post_charge",
            arguments={
                "folioId": NONEXISTENT_FOLIO,
                "description": "layer 5 probe",
                "amount": 1,
                "approval_token": f"forged-{uuid.uuid4().hex}",
            },
        )
        code = _error_code(forged)
        check(
            "a forged approval_token is now refused as APPROVAL_INVALID -- the gate "
            "looked the token up and did not find it, rather than being unable to look",
            code == "APPROVAL_INVALID",
            f"code={code} (was APPROVAL_UNVERIFIABLE before this table existed)",
        )

        # ---- a real approval for a different action ------------------------
        wrong_action = _mint(table, action="void_folio")
        mismatched = client.call_tool_sync(
            tool_use_id="verify-orch-2",
            name="billing___post_charge",
            arguments={
                "folioId": NONEXISTENT_FOLIO,
                "description": "layer 5 probe",
                "amount": 1,
                "approval_token": wrong_action,
            },
        )
        code = _error_code(mismatched)
        check(
            "an approval issued to void a folio cannot be spent on posting a charge",
            code == "APPROVAL_MISMATCH",
            f"code={code}",
        )

        # ---- an expired approval ------------------------------------------
        expired = _mint(table, action="post_charge", expires_in=-60)
        stale = client.call_tool_sync(
            tool_use_id="verify-orch-3",
            name="billing___post_charge",
            arguments={
                "folioId": NONEXISTENT_FOLIO,
                "description": "layer 5 probe",
                "amount": 1,
                "approval_token": expired,
            },
        )
        code = _error_code(stale)
        check(
            "an expired approval is refused even though DynamoDB has not swept it "
            "yet, because the interceptor checks the timestamp itself",
            code == "APPROVAL_INVALID",
            f"code={code}",
        )

        # ---- a valid approval, which must OPEN the gate --------------------
        valid = _mint(table, action="post_charge")
        allowed = client.call_tool_sync(
            tool_use_id="verify-orch-4",
            name="billing___post_charge",
            arguments={
                "folioId": NONEXISTENT_FOLIO,
                "description": "layer 5 probe",
                "amount": 1,
                "approval_token": valid,
            },
        )
        code = _error_code(allowed)
        check(
            "a valid approval lets the call through to the foundation, which then "
            "rejects it on its own terms -- so the gate opens, and nothing moved",
            code not in (
                "APPROVAL_REQUIRED",
                "APPROVAL_INVALID",
                "APPROVAL_MISMATCH",
                "APPROVAL_UNVERIFIABLE",
            ),
            f"code={code}; {_text(allowed)[:200]}",
        )

        # ---- and the token is not consumed by being read -------------------
        again = client.call_tool_sync(
            tool_use_id="verify-orch-5",
            name="billing___post_charge",
            arguments={
                "folioId": NONEXISTENT_FOLIO,
                "description": "layer 5 probe",
                "amount": 1,
                "approval_token": valid,
            },
        )
        check(
            "the interceptor does not consume the token it read, so a Gateway retry "
            "cannot invalidate an approval a human really gave",
            _error_code(again) == _error_code(allowed),
            f"second attempt code={_error_code(again)}",
        )

    for token in (wrong_action, expired, valid):
        ddb.delete_item(TableName=table, Key={"approvalId": {"S": token}})


def _mint(table: str, *, action: str, expires_in: int = 900) -> str:
    """Write an approval the way the Phase-4 console API will.

    Done here with the caller's own credentials on purpose: neither an agent nor an
    interceptor has ``PutItem`` on this table, so a test that could mint a token
    through the agent path would be reporting a privilege escalation.
    """
    token = f"verify-{uuid.uuid4()}"
    ddb.put_item(
        TableName=table,
        Item={
            "approvalId": {"S": token},
            "status": {"S": "APPROVED"},
            "action": {"S": action},
            "expiresAt": {"N": str(int(time.time()) + expires_in)},
            "issuedBy": {"S": "verify_orchestration.py"},
        },
    )
    return token


def _text(result) -> str:
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            return text
    return ""


def _error_code(result) -> str | None:
    try:
        payload = json.loads(_text(result))
    except (TypeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# --------------------------------------------------------------------------- #
# 3. A queued run, end to end
# --------------------------------------------------------------------------- #


def verify_queued_run(out: dict[str, str]) -> None:
    print("\n  -- a queued run, using a deployed schedule's own payload --\n")

    payload = _schedule_payload("hotel-ops-a4-nightaudit")
    if payload is None:
        check("a deployed schedule's payload is readable", False, "no A4 schedule found")
        return

    # A4 is advise-only, so a full verification run writes nothing to the
    # foundation. The run id is ours so the decision log can be queried for it.
    run_id = str(uuid.uuid4())
    payload["runId"] = run_id
    check(
        "the A4 schedule's payload is a complete invocation the invoker accepts",
        bool(payload.get("prompt")) and bool(payload.get("propertyId")),
        json.dumps({k: v for k, v in payload.items() if k != "prompt"})
        + f" prompt={payload.get('prompt', '')[:80]!r}...",
    )

    sqs.send_message(
        QueueUrl=out["InvocationQueueUrl"], MessageBody=json.dumps(payload)
    )
    print(f"        enqueued run {run_id}; waiting up to {RUN_TIMEOUT_SECONDS}s")

    deadline = time.time() + RUN_TIMEOUT_SECONDS
    rows: list[dict] = []
    while time.time() < deadline:
        rows = ddb.query(
            TableName=out["DecisionsTableName"],
            KeyConditionExpression="run_id = :r",
            ExpressionAttributeValues={":r": {"S": run_id}},
            ConsistentRead=True,
        )["Items"]
        if any(r.get("kind", {}).get("S") == "run_summary" for r in rows):
            break
        time.sleep(POLL_SECONDS)

    summary = next((r for r in rows if r.get("kind", {}).get("S") == "run_summary"), None)
    tool_rows = [r for r in rows if r.get("kind", {}).get("S") != "run_summary"]

    check(
        "the invoker drained the queue, ran the agent, and filed a run summary",
        summary is not None,
        f"{len(rows)} rows for run {run_id}"
        + (
            ""
            if summary
            else " -- check /aws/lambda/hotel-ops-agent-invoker and the DLQ"
        ),
    )
    if summary is None:
        return

    check(
        "the run succeeded",
        summary.get("outcome", {}).get("S") == "ok",
        f"outcome={summary.get('outcome', {}).get('S')} "
        f"error={summary.get('error_code', {}).get('S', '-')}",
    )
    check(
        "the summary carries the recommendation, which is the entire product of an "
        "unattended advisory run and exists nowhere else",
        len(summary.get("recommendation", {}).get("S", "")) > 100,
        f"{summary.get('recommendation', {}).get('S', '')[:300]}...",
    )
    check(
        "it records the trigger as a schedule, so an unattended run is "
        "distinguishable from a human's",
        summary.get("trigger", {}).get("S") == "schedule",
        f"trigger={summary.get('trigger', {}).get('S')} "
        f"duration={summary.get('duration_seconds', {}).get('N')}s",
    )
    delegated = [d["S"] for d in summary.get("delegations", {}).get("L", [])]
    check(
        "and which specialist answered",
        "night_audit_agent" in delegated,
        f"delegations={delegated}",
    )

    # The half that has never worked before: the response interceptor finally has a
    # table, so every tool call the run made should be a row.
    check(
        "the Gateway response interceptor logged the run's tool calls -- the first "
        "time it has had a table to write to",
        len(tool_rows) > 0,
        f"{len(tool_rows)} tool rows: "
        + ", ".join(sorted({r.get("tool", {}).get("S", "?") for r in tool_rows})),
    )
    check(
        "each tool row carries the agent and the property from the correlation "
        "headers, not 'unattributed'",
        bool(tool_rows)
        and all(r.get("agent", {}).get("S") != "unattributed" for r in tool_rows)
        and all(
            r.get("property_id", {}).get("S") == payload["propertyId"]
            for r in tool_rows
        ),
        ", ".join(
            sorted({r.get("agent", {}).get("S", "?") for r in tool_rows})
        ),
    )
    check(
        "arguments are hashed, never stored: no row carries a raw arguments blob",
        all("arguments" not in r for r in tool_rows)
        and all(len(r.get("inputs_hash", {}).get("S", "")) == 64 for r in tool_rows),
        "inputs_hash only",
    )
    check(
        "an advise-only run recorded no action_taken, because A4 writes nothing",
        not any("action_taken" in r for r in tool_rows),
        "no writes attributed to a read-only agent",
    )
    check(
        "the summary sorts after every tool call the run made, so one query returns "
        "the trajectory then the conclusion",
        summary["event_id"]["S"]
        == max(r["event_id"]["S"] for r in rows),
        f"summary event_id={summary['event_id']['S']}",
    )


def _schedule_payload(name_prefix: str) -> dict | None:
    listed = scheduler.list_schedules(GroupName="hotel-ops-agent")["Schedules"]
    match = next((s for s in listed if s["Name"].startswith(name_prefix)), None)
    if match is None:
        return None
    described = scheduler.get_schedule(
        GroupName="hotel-ops-agent", Name=match["Name"]
    )
    return json.loads(described["Target"]["Input"])


# --------------------------------------------------------------------------- #
# 4. The reactive rules, against real event shapes
# --------------------------------------------------------------------------- #

#: Copied field-for-field from the foundation's own ``publish_event`` calls --
#: ``crs/create_reservation.py`` and ``pms/checkinout/check_out.py`` -- because a
#: pattern tested against an invented event shape proves nothing about the bus.
def _sample_events(property_id: str) -> dict[str, dict]:
    return {
        "hotel-ops-reservation-created-to-a1": {
            "source": "anycompany.reservations",
            "detail-type": "reservation.created",
            "detail": {
                "reservationId": "res-1",
                "confirmationNumber": "ANY123",
                "propertyId": property_id,
                "guestId": "guest-1",
                "roomTypeId": "type-1",
                "ratePlanId": "rate-1",
                "checkInDate": "2026-09-20",
                "checkOutDate": "2026-09-22",
                "totalAfterTax": "412.00",
                "currency": "USD",
                "status": "CONFIRMED",
                "_metadata": {"correlationId": "corr-1"},
            },
        },
        "hotel-ops-checked-out-to-a2": {
            "source": "anycompany.pms",
            "detail-type": "checkinout.checked_out",
            "detail": {
                "reservationId": "res-2",
                "propertyId": property_id,
                "guestId": "guest-2",
                "roomId": "room-2",
                "roomNumber": "1010",
                "expressCheckout": False,
                "_metadata": {"correlationId": "corr-2"},
            },
        },
    }


#: The platform's stack name. Overridable because it is chosen at `sam deploy` time.
FOUNDATION_STACK = os.environ.get("HOTEL_OPS_FOUNDATION_STACK", "anycompany-booking")


def _foundation_output(key: str) -> str:
    """One output of the platform's stack. Resolved, never hardcoded.

    Every identifier in this file used to be a literal from the account it was written
    in, which made the script a no-op anywhere else.
    """
    for o in cfn.describe_stacks(StackName=FOUNDATION_STACK)["Stacks"][0].get(
        "Outputs", []
    ):
        if o["OutputKey"] == key:
            return o["OutputValue"]
    raise SystemExit(
        f"{FOUNDATION_STACK} has no {key} output. Set HOTEL_OPS_FOUNDATION_STACK if "
        "your platform stack is named differently."
    )


def _foundation_bus() -> str:
    """The platform's custom event bus, derived the same way the CDK app derives it."""
    environment = os.environ.get("HOTEL_OPS_ENVIRONMENT", "dev")
    return f"anycompany-events-{environment}"


def _envelope(sample: dict) -> dict:
    """Wrap a detail payload in the envelope EventBridge delivers it in.

    ``TestEventPattern`` validates the envelope strictly and rejects an ISO
    timestamp with microseconds or a numeric offset, so ``time`` is formatted the
    way the service itself emits it.
    """
    return {
        **sample,
        "id": str(uuid.uuid4()),
        "version": "0",
        # TestEventPattern needs *an* account id, not the real one: it matches the
        # pattern only, and nothing is delivered. The AWS documentation placeholder.
        "account": "111122223333",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": REGION,
        "resources": [],
    }


def verify_rules(property_id: str) -> None:
    print("\n  -- the reactive rules, against the foundation's real event shapes --\n")
    bus = _foundation_bus()
    samples = _sample_events(property_id)
    # A3 rides the same event as A2, so it is checked against that shape too.
    samples["hotel-ops-checked-out-to-a3"] = samples["hotel-ops-checked-out-to-a2"]

    for rule_name, sample in samples.items():
        try:
            rule = events.describe_rule(EventBusName=bus, Name=rule_name)
        except events.exceptions.ResourceNotFoundException:
            check(f"{rule_name} exists on the foundation's bus", False, "not found")
            continue

        matched = events.test_event_pattern(
            EventPattern=rule["EventPattern"], Event=json.dumps(_envelope(sample))
        )["Result"]
        check(
            f"{rule_name} matches a real "
            f"{sample['detail-type']} event from {sample['source']}",
            matched,
            f"state={rule['State']}; pattern={rule['EventPattern'][:150]}",
        )

    # The scope bound: an event from another property must not start a run.
    other = _envelope(
        _sample_events("11111111-1111-1111-1111-111111111111")[
            "hotel-ops-checked-out-to-a2"
        ]
    )
    rule = events.describe_rule(EventBusName=bus, Name="hotel-ops-checked-out-to-a2")
    check(
        "and an event from a non-pilot property does not match, so the 49 other "
        "hotels do not each start an agent run every time the simulator fires",
        not events.test_event_pattern(
            EventPattern=rule["EventPattern"], Event=json.dumps(other)
        )["Result"],
        "filtered at the bus, before any compute",
    )

    states = {
        r["Name"]: r["State"]
        for r in events.list_rules(EventBusName=bus, NamePrefix="hotel-ops-")["Rules"]
    }
    schedules = {
        s["Name"]: s["State"]
        for s in scheduler.list_schedules(GroupName="hotel-ops-agent")["Schedules"]
    }
    print(
        f"\n        triggers: {len(states)} rules, {len(schedules)} schedules\n"
        f"        rules:     {json.dumps(states)}\n"
        f"        schedules: {json.dumps(schedules)}"
    )


# --------------------------------------------------------------------------- #
# 6. Non-interference, run every time because these rules touch a shared bus
# --------------------------------------------------------------------------- #


def verify_non_interference() -> None:
    print("\n  -- Layer 6: the foundation is unchanged --\n")
    stack = cfn.describe_stacks(StackName="anycompany-booking")["Stacks"][0]
    check(
        "anycompany-booking is untouched: adding rules to its bus does not modify "
        "its stack",
        stack["StackStatus"] == "UPDATE_COMPLETE",
        f"{stack['StackStatus']}, last updated {stack['LastUpdatedTime'].isoformat()}",
    )
    groups = boto3.client("cognito-idp", region_name=REGION).list_groups(
        UserPoolId=_foundation_output("UserPoolId")
    )["Groups"]
    check(
        "and its Cognito groups are unchanged at 6",
        len(groups) == 6,
        ", ".join(sorted(g["GroupName"] for g in groups)),
    )
    dlq = sqs.get_queue_attributes(
        QueueUrl=outputs(STACK)["InvocationDlqUrl"],
        AttributeNames=["ApproximateNumberOfMessages"],
    )["Attributes"]
    check(
        "the invocation DLQ is empty",
        dlq["ApproximateNumberOfMessages"] == "0",
        f"{dlq['ApproximateNumberOfMessages']} messages",
    )


# --------------------------------------------------------------------------- #


def main() -> int:
    with_model = "--no-model" not in sys.argv
    out = outputs(STACK)

    verify_tables(out)

    payload = _schedule_payload("hotel-ops-a4-nightaudit") or {}
    property_id = payload.get("propertyId")
    if not property_id:
        raise SystemExit(
            "No deployed schedule carries a propertyId. Deploy with "
            "-c pilotPropertyIds=<uuid> first."
        )

    if with_model:
        # No model in this one -- it is the interceptor and the foundation -- but it
        # does reach the Gateway, so it is grouped with the paid checks.
        verify_approval_gate(out)
        verify_queued_run(out)
    else:
        print("\n  (skipping the gate and the queued run: --no-model)\n")

    verify_rules(property_id)
    verify_non_interference()

    failed = [label for ok, label in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
