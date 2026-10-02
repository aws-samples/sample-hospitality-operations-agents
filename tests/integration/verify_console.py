#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Layer 4 verification: the approval loop, end to end, against the deployed API.

The plan calls this the critical safety test and says that if it passes in the
wrong direction, stop and fix it before anything else ships. So it is written to
try to get money moved without a human, from several angles, and to then prove that
a human genuinely can.

What is checked, in order:

1. **The authorizer.** No token, a garbage token, and an *access* token instead of
   an ID token must all be refused by API Gateway before any of our code runs.
2. **Authorization inside the API.** A Housekeeping account, scoped to one property,
   cannot read another property's runs and cannot approve anything. A Manager can do
   both. This is the foundation's own ``verify_property_access`` model, mirrored.
3. **The approval loop.** File a proposal, approve it as a Manager, and confirm the
   agent is re-invoked and the gate opens -- proved by the *foundation* rejecting the
   charge on its own terms, which can only happen if the call got that far.
4. **The bindings.** The released token is then presented, through the Gateway
   directly with no model in the path, for a different folio and a different amount.
   Both must be refused. This is the check that stops one approval becoming a
   general licence to bill.
5. **Idempotency of the decision.** Approving twice must lose the race, not
   overwrite a colleague's judgment.

Nothing here can move real money: every proposal targets a folio id of all zeroes,
which does not exist, so the furthest a released approval can get is the
foundation's own 404.

Test credentials are the *agent* users this project already created -- a Manager
(``agent-arrivals``) and a per-property Housekeeping account. They are real Cognito
users in the right groups, so no console account has to be invented to test the
console, and the plan's "new Cognito users only" limit is not stretched further.

Usage::

    AWS_PROFILE=... tests/integration/verify_console.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")
API_STACK = "hotel-ops-agent-api"
ORCH_STACK = "hotel-ops-agent-orchestration"

#: A folio that does not exist. The gate opening is proved by the foundation's 404,
#: not by a charge landing.
NONEXISTENT_FOLIO = "00000000-0000-0000-0000-000000000000"
PROPOSED_AMOUNT = 41.5
#: Bound by both gates, like the amount, so every probe that should pass sends exactly this.
PROPOSED_DESCRIPTION = "Layer 4 verification probe"

RUN_TIMEOUT_SECONDS = 300
POLL_SECONDS = 10

cfn = boto3.client("cloudformation", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
idp = boto3.client("cognito-idp", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)

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
# Credentials and HTTP
# --------------------------------------------------------------------------- #


def _admin_client_id() -> str:
    """The platform's ADMIN_USER_PASSWORD_AUTH client, read from its own stack.

    Resolved rather than hardcoded: it is the only client permitting that flow, and
    its id differs per deployment. A literal here would work in exactly one account.
    """
    stack = os.environ.get("HOTEL_OPS_FOUNDATION_STACK", "anycompany-booking")
    for o in cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", []):
        if o["OutputKey"] == "AdminAuthClientId":
            return o["OutputValue"]
    raise SystemExit(
        f"{stack} has no AdminAuthClientId output. Set HOTEL_OPS_FOUNDATION_STACK if "
        "your platform stack is named differently."
    )


def tokens_for(agent: str) -> dict[str, str]:
    """Sign in as one of this project's agent identities.

    Uses the platform's admin auth client, the same one ``foundation_client.py`` uses,
    because it is the only client permitting ``ADMIN_USER_PASSWORD_AUTH``. Returns
    both tokens so the authorizer can be tested with the wrong one.
    """
    creds = json.loads(
        sm.get_secret_value(SecretId=f"hotel-ops-agent/{agent}")["SecretString"]
    )
    pool = outputs(API_STACK)["ConsoleUserPoolId"]
    result = idp.admin_initiate_auth(
        UserPoolId=pool,
        ClientId=_admin_client_id(),
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": creds["username"], "PASSWORD": creds["password"]},
    )["AuthenticationResult"]
    return {"id": result["IdToken"], "access": result["AccessToken"]}


def housekeeping_tokens(property_id: str) -> dict[str, str]:
    """The per-property housekeeping identity: one property, non-approver group."""
    creds = json.loads(
        sm.get_secret_value(SecretId="hotel-ops-agent/housekeeping")["SecretString"]  # pragma: allowlist secret (a secret name, not a value)
    )
    username = creds["username"].replace("{property_id}", property_id)
    result = idp.admin_initiate_auth(
        UserPoolId=outputs(API_STACK)["ConsoleUserPoolId"],
        ClientId=_admin_client_id(),
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": creds["password"]},
    )["AuthenticationResult"]
    return {"id": result["IdToken"], "access": result["AccessToken"]}


def call(
    base: str, method: str, path: str, token: str | None = None, body: dict | None = None
) -> tuple[int, dict]:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            **({"Authorization": token} if token else {}),
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request) as response:  # nosec B310 - base is the API stack output, https
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")[:300]}


def _pilot_property() -> str:
    """A property to test against, taken from a deployed schedule's own payload.

    So the script needs no environment-specific constant: whatever property the
    schedules were deployed for is the one that has runs to read.
    """
    scheduler = boto3.client("scheduler", region_name=REGION)
    listed = scheduler.list_schedules(GroupName="hotel-ops-agent")["Schedules"]
    for summary in listed:
        target = scheduler.get_schedule(
            GroupName="hotel-ops-agent", Name=summary["Name"]
        )["Target"]
        payload = json.loads(target["Input"])
        if payload.get("propertyId"):
            return payload["propertyId"]
    raise SystemExit(
        "No deployed schedule carries a propertyId. Set HOTEL_OPS_PILOT_PROPERTY, or "
        "deploy with -c pilotPropertyIds=<uuid>."
    )


def code_of(payload: dict) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    return error.get("code") if isinstance(error, dict) else None


# --------------------------------------------------------------------------- #
# 1. The authorizer
# --------------------------------------------------------------------------- #


def verify_authorizer(base: str, manager: dict) -> None:
    print("=" * 72)
    print("Layer 4: the approval loop")
    print("=" * 72)
    print("\n  -- the authorizer, before any of our code runs --\n")

    status, _ = call(base, "GET", "/runs")
    check(
        "an unauthenticated request is refused by API Gateway",
        status == 401,
        f"status={status}",
    )

    status, _ = call(base, "GET", "/runs", token="not-a-jwt")  # nosec B106 - deliberately invalid
    check("a garbage token is refused", status == 401, f"status={status}")

    # The access token is a valid JWT from the same pool, so this is not merely a
    # signature check: it distinguishes the token that carries cognito:groups and
    # custom:property_id from the one that does not.
    status, payload = call(base, "GET", "/runs", token=manager["access"])
    check(
        "an access token is refused where an ID token is required, because only the "
        "ID token carries the groups the whole authorization model reads",
        status == 401,
        f"status={status} {code_of(payload) or ''}",
    )

    status, payload = call(base, "GET", "/runs?limit=3", token=manager["id"])
    check(
        "a Manager's ID token is accepted",
        status == 200 and payload.get("success") is True,
        f"status={status}, {payload.get('data', {}).get('count')} runs",
    )


# --------------------------------------------------------------------------- #
# 2. Authorization inside the API
# --------------------------------------------------------------------------- #


def verify_scoping(base: str, manager: dict, housekeeper: dict, property_id: str) -> None:
    print("\n  -- property scope and approver authority --\n")

    status, payload = call(
        base, "GET", f"/runs?propertyId={property_id}", token=housekeeper["id"]
    )
    check(
        "a property-scoped account can read its own property's runs",
        status == 200,
        f"status={status} count={payload.get('data', {}).get('count')}",
    )

    other = "11111111-1111-1111-1111-111111111111"
    status, payload = call(base, "GET", f"/runs?propertyId={other}", token=housekeeper["id"])
    check(
        "and is refused another property's, rather than being silently shown its own",
        status == 403 and code_of(payload) == "OUT_OF_SCOPE",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(base, "GET", "/approvals", token=housekeeper["id"])
    check(
        "it may read the approval queue -- seeing what is pending is not privileged",
        status == 200 and payload["data"]["youMayApprove"] is False,
        f"youMayApprove={payload.get('data', {}).get('youMayApprove')}",
    )

    status, payload = call(base, "GET", "/approvals", token=manager["id"])
    check(
        "and a Manager is told it may approve",
        status == 200 and payload["data"]["youMayApprove"] is True,
        f"youMayApprove={payload.get('data', {}).get('youMayApprove')}",
    )


# --------------------------------------------------------------------------- #
# 3 + 4 + 5. The loop, the bindings, the race
# --------------------------------------------------------------------------- #


def verify_approval_loop(
    base: str,
    manager: dict,
    filer: dict,
    housekeeper: dict,
    property_id: str,
    gateway_url: str,
) -> None:
    """``filer`` and ``manager`` are both Managers and deliberately different people:
    an approver may not release their own proposal."""
    print("\n  -- filing and releasing a proposal --\n")

    status, payload = call(
        base,
        "POST",
        "/approvals",
        token=filer["id"],
        body={
            "action": "post_charge",
            "folioId": NONEXISTENT_FOLIO,
            "amount": PROPOSED_AMOUNT,
            "propertyId": property_id,
            "reason": "no description supplied",
        },
    )
    check(
        "a proposal missing an argument the tool requires is refused at filing time, "
        "not discovered after a human has already approved it",
        status == 400 and code_of(payload) == "MISSING_ARGUMENT",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        "/approvals",
        token=filer["id"],
        body={
            "action": "post_charge",
            "folioId": NONEXISTENT_FOLIO,
            "amount": PROPOSED_AMOUNT,
            # Required at filing time, and that requirement is itself a finding: the
            # first version of this test omitted it, the agent was released to post
            # the charge, discovered post_charge needs a description, and stopped to
            # ask a human -- after a human had already approved. A proposal must
            # carry everything the tool needs.
            "description": PROPOSED_DESCRIPTION,
            "propertyId": property_id,
            "reason": "Layer 4 verification. This folio does not exist.",
        },
    )
    if not check(
        "a proposal can be filed, and comes back PENDING",
        status == 200 and payload["data"]["approval"]["status"] == "PENDING",
        f"status={status} {json.dumps(payload)[:220]}",
    ):
        return
    proposal_id = payload["data"]["approval"]["id"]

    # The token must never leave the API except to the agent. A queue endpoint that
    # handed out spendable tokens to everyone who can read the queue would make the
    # approver group decorative.
    status, listed = call(base, "GET", "/approvals", token=housekeeper["id"])
    serialized = json.dumps(listed)
    check(
        "no response to a queue reader contains the approval token",
        "apv-" not in serialized,
        "approvalId is withheld from every list response",
    )

    status, payload = call(
        base,
        "POST",
        f"/approvals/{proposal_id}/approve",
        token=housekeeper["id"],
        body={"note": "trying to release money without the authority to"},
    )
    check(
        "a non-approver cannot release it",
        status == 403 and code_of(payload) == "NOT_AN_APPROVER",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        f"/approvals/{proposal_id}/approve",
        token=filer["id"],
        body={"note": "approving my own proposal"},
    )
    check(
        "the person who filed it cannot release it themselves, even as an approver",
        status == 403 and code_of(payload) == "SELF_APPROVAL",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        f"/approvals/{proposal_id}/approve",
        token=manager["id"],
        body={},
    )
    check(
        "and an approver cannot release it without saying why",
        status == 400 and code_of(payload) == "MISSING_NOTE",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        f"/approvals/{proposal_id}/approve",
        token=manager["id"],
        body={"note": "Verified against the folio. Layer 4."},
    )
    if not check(
        "a Manager releases it, and the agent is re-invoked to execute it",
        status == 200 and payload["data"]["status"] == "APPROVED"
        and bool(payload["data"].get("executionRunId")),
        f"status={status} run={payload.get('data', {}).get('executionRunId')}",
    ):
        return
    execution_run_id = payload["data"]["executionRunId"]

    status, payload = call(
        base,
        "POST",
        f"/approvals/{proposal_id}/approve",
        token=manager["id"],
        body={"note": "second approver"},
    )
    check(
        "approving twice loses the race rather than overwriting the first judgment",
        status == 409 and code_of(payload) == "ALREADY_DECIDED",
        f"status={status} code={code_of(payload)}",
    )

    _verify_bindings(proposal_id, gateway_url, property_id)
    _verify_execution(base, manager, execution_run_id)


def _token_of(proposal_id: str) -> str | None:
    """Read the minted token straight from the table.

    Deliberately out of band. No API response returns it, which is the point of the
    previous check -- so the only way to test what the token can and cannot do is to
    read it with AWS credentials no console user has.
    """
    approvals = outputs(ORCH_STACK)["ApprovalsTableName"]
    kwargs = {
        "TableName": approvals,
        "FilterExpression": "proposalId = :p",
        "ExpressionAttributeValues": {":p": {"S": proposal_id}},
    }
    while True:
        response = ddb.scan(**kwargs)
        for item in response.get("Items", []):
            return item["approvalId"]["S"]
        if "LastEvaluatedKey" not in response:
            return None
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _verify_bindings(proposal_id: str, gateway_url: str, property_id: str) -> None:
    """The released token, presented at the Gateway with no model in the path.

    Presented the way the Runtime presents it on an execution run: as the
    ``X-Hotel-Ops-Approval-Token`` header, on a run pinned to the proposal's property.
    Never as a tool argument -- the interceptor ignores that, and the first check
    below proves it.
    """
    print("\n  -- what the released token can and cannot do --\n")

    token = _token_of(proposal_id)
    if token is None:
        check("the minted token is readable for testing", False, "not found in table")
        return

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import importlib

    importlib.import_module("verify_gateway").load_environment()
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agents")
    )
    gateway = importlib.import_module("gateway")
    run_context = importlib.import_module("run_context")
    today = datetime.now(timezone.utc).date().isoformat()

    # First: the real token, but only in the arguments, where a model would put it.
    run_context.set_current(
        run_context.RunContext(
            property_id=property_id, operating_date=today, trigger="chat",
            caller_groups=("Manager",),
        )
    )
    bare = gateway._client_for("billing", gateway.correlation_headers("billing"))
    with bare:
        result = bare.call_tool_sync(
            tool_use_id="verify-console-token-as-argument",
            name="billing___post_charge",
            arguments={
                "propertyId": property_id,
                "folioId": NONEXISTENT_FOLIO,
                "amount": PROPOSED_AMOUNT,
                "description": PROPOSED_DESCRIPTION,
                "approval_token": token,
            },
        )
        texts = [b.get("text") for b in result.get("content") or [] if isinstance(b, dict)]
        code = next((code_of(json.loads(t)) for t in texts if isinstance(t, str)), None)
    check(
        "even the real token opens nothing when it arrives as an argument, which is "
        "the only channel a model has",
        code == "APPROVAL_REQUIRED",
        f"code={code}",
    )

    run_context.set_current(
        run_context.RunContext(
            property_id=property_id, operating_date=today, trigger="chat",
            caller_groups=("Manager",), approval_token=token,
        )
    )
    client = gateway._client_for("billing", gateway.correlation_headers("billing"))
    with client:

        def attempt(label: str, arguments: dict) -> str | None:
            result = client.call_tool_sync(
                tool_use_id=f"verify-console-{label}",
                name="billing___post_charge",
                arguments={**arguments, "propertyId": property_id},
            )
            for block in result.get("content") or []:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str):
                    try:
                        return code_of(json.loads(text))
                    except ValueError:
                        return None
            return None

        code = attempt(
            "other-folio",
            {
                "folioId": "99999999-9999-9999-9999-999999999999",
                "amount": PROPOSED_AMOUNT,
                "description": PROPOSED_DESCRIPTION,
            },
        )
        check(
            "the token cannot be spent on a folio the approval did not name",
            code == "APPROVAL_MISMATCH",
            f"code={code}",
        )

        code = attempt(
            "other-amount",
            {
                "folioId": NONEXISTENT_FOLIO,
                "amount": PROPOSED_AMOUNT * 100,
                "description": PROPOSED_DESCRIPTION,
            },
        )
        check(
            "nor for an amount the approval did not name",
            code == "APPROVAL_MISMATCH",
            f"code={code} (approved {PROPOSED_AMOUNT}, presented "
            f"{PROPOSED_AMOUNT * 100})",
        )

        code = attempt(
            "other-description",
            {
                "folioId": NONEXISTENT_FOLIO,
                "amount": PROPOSED_AMOUNT,
                "description": "a line nobody approved",
            },
        )
        check(
            "nor with a folio description the approval did not name -- every captured "
            "argument is bound, not just the money",
            code == "APPROVAL_MISMATCH",
            f"code={code}",
        )

        code = attempt(
            "as-approved",
            {
                "folioId": NONEXISTENT_FOLIO,
                "amount": PROPOSED_AMOUNT,
                "description": PROPOSED_DESCRIPTION,
            },
        )
        check(
            "and exactly as approved it reaches the foundation, which rejects it on "
            "its own terms -- so the gate opens, and nothing moved",
            code == "NOT_FOUND",
            f"code={code}",
        )


def _verify_execution(base: str, manager: dict, run_id: str) -> None:
    """The agent's own attempt to spend the approval it was handed."""
    print(f"\n  -- the execution run the approval triggered ({run_id}) --\n")

    deadline = time.time() + RUN_TIMEOUT_SECONDS
    payload: dict = {}
    while time.time() < deadline:
        status, payload = call(base, "GET", f"/runs/{run_id}", token=manager["id"])
        if status == 200 and payload["data"].get("status") == "complete":
            break
        time.sleep(POLL_SECONDS)

    data = payload.get("data") or {}
    if not check(
        "the approval re-invoked the agent and the run completed",
        data.get("status") == "complete",
        f"status={data.get('status')} steps={data.get('stepCount')}",
    ):
        return

    steps = data.get("steps") or []
    charge_attempts = [s for s in steps if (s.get("tool") or "").endswith("post_charge")]

    # Two outcomes are correct here, and which one happens varies by run.
    #
    # The agent may execute the approved charge -- and this test's folio does not
    # exist, so the platform answers 404 and nothing moves. Or A3 may refuse before
    # calling anything, which it has done, with reasons worth reading: the approval
    # is attributed to `agent-arrivals@anycompany.internal`, which is an agent
    # identity rather than a human operator, and the folio is all zeroes and it will
    # not post against a target it has not read line by line. Both objections are
    # correct, and both are artefacts of how this test has to be built -- it cannot
    # use a real human's account or a real folio without moving real money.
    #
    # So this asserts the invariant rather than the path: nothing was written, and if
    # the agent did call the tool, no approval error came back. That the *gate opens*
    # is proved deterministically in _verify_bindings, with no model in the path at
    # all, which is where that proof belongs.
    check(
        "no approval error came back from any charge the agent did attempt",
        not any(
            (s.get("errorCode") or "").startswith("APPROVAL_") for s in charge_attempts
        ),
        f"{len(charge_attempts)} charge attempts; codes: "
        + (", ".join(sorted({s.get("errorCode") or "-" for s in charge_attempts})) or "none"),
    )
    check(
        "nothing was written, whichever way the run went",
        not any(s.get("actionTaken") for s in steps),
        f"{len(steps)} steps, 0 actions taken"
        + ("" if charge_attempts else " (A3 declined before calling the tool)"),
    )
    print(f"        answer: {' '.join((data.get('answer') or '').split())[:300]}")


# --------------------------------------------------------------------------- #
# The override, which is Phase 5's ground truth
# --------------------------------------------------------------------------- #


def verify_override(base: str, manager: dict, housekeeper: dict) -> None:
    print("\n  -- the human verdict the evaluators will score against --\n")

    status, payload = call(base, "GET", "/runs?limit=1", token=manager["id"])
    runs = (payload.get("data") or {}).get("runs") or []
    if not runs:
        check("a run exists to record a verdict on", False, "no runs found")
        return
    run_id = runs[0]["runId"]

    status, payload = call(
        base, "POST", f"/runs/{run_id}/override", token=manager["id"], body={}
    )
    check(
        "an override without a reason is refused -- the bare fact of disagreement is "
        "a weak signal",
        status == 400 and code_of(payload) == "MISSING_REASON",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        f"/runs/{run_id}/override",
        token=manager["id"],
        body={"verdict": "sort-of", "reason": "x"},
    )
    check(
        "and an unrecognized verdict is refused rather than stored as free text",
        status == 400 and code_of(payload) == "INVALID_VERDICT",
        f"status={status} code={code_of(payload)}",
    )

    status, payload = call(
        base,
        "POST",
        f"/runs/{run_id}/override",
        token=manager["id"],
        body={"verdict": "endorsed", "reason": "Layer 4 verification; reviewed."},
    )
    override = (payload.get("data") or {}).get("humanOverride") or {}
    check(
        "a verdict is recorded with who gave it, taken from the token",
        status == 200 and override.get("verdict") == "endorsed"
        and "@" in (override.get("by") or ""),
        f"{json.dumps(override)[:220]}",
    )

    status, payload = call(base, "GET", f"/runs/{run_id}", token=manager["id"])
    check(
        "and it reads back on the run",
        (payload.get("data") or {}).get("humanOverride", {}).get("verdict") == "endorsed",
        "stored on the summary row, so one query returns the run and the verdict",
    )


# --------------------------------------------------------------------------- #


def main() -> int:
    api = outputs(API_STACK)
    base = api["ConsoleApiUrl"].rstrip("/")
    property_id = os.environ.get("HOTEL_OPS_PILOT_PROPERTY") or _pilot_property()
    gateway_url = outputs("hotel-ops-agent-agentcore")["GatewayUrl"]

    manager = tokens_for("arrivals")
    # A second Manager, so a proposal can be filed by one person and released by
    # another -- the console refuses self-approval.
    filer = tokens_for("billing")
    housekeeper = housekeeping_tokens(property_id)

    verify_authorizer(base, manager)
    verify_scoping(base, manager, housekeeper, property_id)
    verify_approval_loop(base, manager, filer, housekeeper, property_id, gateway_url)
    verify_override(base, manager, housekeeper)

    failed = [label for ok, label in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
