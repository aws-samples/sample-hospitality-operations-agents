#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Layer 2 verification: the agent graph against the *deployed* Gateway.

Layer 1 (``verify_tools.py``) proved each tool Lambda in isolation, but it
invoked them with a synthetic Gateway event -- so it can only prove that the
handler agrees with itself about the tool-name contract. It cannot prove that
either of them agrees with the Gateway. That gap is not hypothetical: the Gateway
delimits ``{target}___{tool}`` with **three** underscores, the code assumed two,
and every Layer 1 check passed while every real dispatch failed with
``UNKNOWN_TOOL``. This file closes that gap by talking to the real endpoint.

Four things are checked, cheapest first:

1. **Tool discovery** -- each target advertises its own tools and only its own,
   through ``agents/gateway.py``'s real filter. This is the assertion that
   ``hotel-operations-agent.md`` §3's isolation claim rests on.
2. **Real dispatch** -- one read tool executed end to end: Gateway -> request
   interceptor -> Lambda -> Cognito -> foundation API -> back. A tool list that
   looks right proves nothing about dispatch.
3. **The approval gate at the tool plane** -- ``billing___post_charge`` refused by
   the Gateway's request interceptor, with no model anywhere in the path. This is
   the mechanical half of Layer 4: the model cannot talk its way past something
   that runs outside the model.
4. **Routing** -- the orchestrator sends a room-assignment question to
   ``arrivals_agent`` and not to ``billing_agent``. This one invokes Sonnet and
   therefore costs money; ``--no-model`` skips it.

Usage::

    AWS_PROFILE=... tests/integration/verify_gateway.py [--no-model]

Environment is read from the deployed stack's outputs, so there is nothing to
configure. ``HOTEL_OPS_REGION`` overrides the region (default ``us-east-1``);
the ambient ``AWS_REGION`` is deliberately ignored, because this profile's is
``us-west-2`` and everything this touches lives in ``us-east-1``.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK = "hotel-ops-agent-agentcore"
REGION = os.environ.get("HOTEL_OPS_REGION", "us-east-1")

#: Tools each target is expected to advertise. Exact counts, not lower bounds: a
#: target that grows a tool nobody wired into a sub-agent's prompt, or loses one,
#: should fail here rather than be discovered by a model at 3 a.m.
EXPECTED_TOOL_COUNT = {
    "arrivals": 5,
    "housekeeping": 6,
    "billing": 8,
    "nightaudit": 6,
    "regional": 4,
}

#: The sub-agent name that delegates to each target. A4's two spellings differ,
#: which is exactly why gateway.tools_for takes the agent name explicitly.
AGENT_OF = {
    "arrivals": "arrivals",
    "housekeeping": "housekeeping",
    "billing": "billing",
    "nightaudit": "night_audit",
    "regional": "regional",
}

DELIMITER = "___"

#: Ids that exist nowhere. Every probe that could write uses them, so a gate that
#: ever failed open would still have nothing to charge or assign.
NO_SUCH_ID = "00000000-0000-0000-0000-000000000000"
ANOTHER_HOTEL = "11111111-1111-4111-8111-111111111111"


# --------------------------------------------------------------------------- #
# Environment, from the deployed stack
# --------------------------------------------------------------------------- #


def load_environment() -> dict[str, str]:
    """Populate the same variables the Runtime's environment_variables carry.

    Read from CloudFormation rather than hardcoded so this file cannot drift from
    what is actually deployed -- the whole point of Layer 2 is to stop trusting
    local assumptions about remote state.
    """
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in boto3.client("cloudformation", region_name=REGION)
        .describe_stacks(StackName=STACK)["Stacks"][0]
        .get("Outputs", [])
    }
    missing = [k for k in ("GatewayUrl", "MemoryId", "CodeInterpreterId") if k not in outputs]
    if missing:
        raise SystemExit(f"{STACK} is missing output(s): {', '.join(missing)}")

    env = {
        "GATEWAY_URL": outputs["GatewayUrl"],
        "MEMORY_ID": outputs["MemoryId"],
        "CODE_INTERPRETER_ID": outputs["CodeInterpreterId"],
        # Must match the templates agentcore_stack gave the memory strategies, or
        # retrieval reads a namespace nothing was ever written to.
        "MEMORY_SEMANTIC_NAMESPACE": "/hotel-ops/facts/{actorId}",
        "MEMORY_SUMMARY_NAMESPACE": "/hotel-ops/shift/{actorId}/{sessionId}",
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "WARNING"),
    }
    os.environ.update(env)
    return env


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #

results: list[tuple[bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))
    return ok


def text_of(result) -> str:
    """The tool's payload. The tool layer returns the foundation's envelope as
    one text block, verbatim.

    ``call_tool_sync`` returns an ``MCPToolResult``, which is a TypedDict --
    ``{"toolUseId", "status", "content"}`` -- not an object, so this reads keys.
    """
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


def envelope_of(result) -> dict:
    try:
        payload = json.loads(text_of(result))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- #
# 1 + 2 + 3: the tool plane
# --------------------------------------------------------------------------- #


def verify_tool_plane(gateway) -> str | None:
    """Returns a real propertyId for the routing check, or None."""
    print("=" * 72)
    print("Layer 2: the deployed Gateway")
    print("=" * 72)

    advertised: dict[str, list[str]] = {}
    for target, expected in EXPECTED_TOOL_COUNT.items():
        try:
            with gateway.tools_for(target, agent=AGENT_OF[target]) as tools:
                names = sorted(tool.tool_name for tool in tools)
        except Exception as exc:  # noqa: BLE001 - the failure *is* the result
            check(f"{target}: tools are discoverable", False, f"{type(exc).__name__}: {exc}")
            continue
        advertised[target] = names
        check(
            f"{target}: advertises its {expected} tools and only its own",
            len(names) == expected
            and all(n.startswith(f"{target}{DELIMITER}") for n in names),
            ", ".join(names),
        )

    check(
        "every tool name uses the three-underscore delimiter the code parses",
        bool(advertised)
        and all(
            name.split(DELIMITER, 1)[0] == target and DELIMITER in name
            for target, names in advertised.items()
            for name in names
        ),
        "no name relies on a two-underscore reading",
    )

    # ---- 2. Real dispatch, all the way to the foundation -------------------
    property_id = None
    client = gateway._client_for("regional", gateway.correlation_headers("regional"))
    with client:
        result = client.call_tool_sync(
            tool_use_id="verify-gateway-1", name=f"regional{DELIMITER}list_properties"
        )
        envelope = envelope_of(result)
        properties = (envelope.get("data") or {}).get("properties") or []
        check(
            "a real tool call dispatches through the Gateway to the foundation "
            "and returns the envelope verbatim",
            result.get("status") == "success" and envelope.get("success") is True and bool(properties),
            f"{len(properties)} properties; {text_of(result)[:160]}",
        )
        if properties:
            property_id = properties[0]["propertyId"]

    # ---- 3. The approval gate, with no model in the path -------------------
    print("\n  -- the Tier-2 gate, enforced outside the model --\n")
    client = gateway._client_for("billing", gateway.correlation_headers("billing"))
    with client:
        result = client.call_tool_sync(
            tool_use_id="verify-gateway-2",
            name=f"billing{DELIMITER}post_charge",
            arguments={
                "propertyId": property_id or NO_SUCH_ID,
                # A folio id that does not exist: if the gate ever fails open,
                # this must not be a chargeable folio.
                "folioId": NO_SUCH_ID,
                "description": "layer 2 probe",
                "amount": 1,
            },
        )
        envelope = envelope_of(result)
        code = (envelope.get("error") or {}).get("code")
        check(
            "the Gateway's request interceptor refuses billing___post_charge "
            "with no approval_token",
            result.get("status") == "error" and code == "APPROVAL_REQUIRED",
            f"status={result.get('status')} code={code}; {text_of(result)[:200]}",
        )
        check(
            "the refusal reaches the caller as a readable tool error, so the "
            "model can file a proposal instead of losing its turn",
            bool(text_of(result)) and envelope.get("success") is False,
            "isError result, not a JSON-RPC transport error",
        )

        result = client.call_tool_sync(
            tool_use_id="verify-gateway-3",
            name=f"billing{DELIMITER}post_charge",
            arguments={
                "propertyId": property_id or NO_SUCH_ID,
                "folioId": NO_SUCH_ID,
                "description": "layer 2 probe",
                "amount": 1,
                "approval_token": "forged-not-a-real-approval",
            },
        )
        envelope = envelope_of(result)
        code = (envelope.get("error") or {}).get("code")
        check(
            "an approval_token written into the arguments -- where a model would "
            "put one -- is ignored, so the call is refused as unapproved",
            result.get("status") == "error" and code == "APPROVAL_REQUIRED",
            f"code={code}; {text_of(result)[:200]}",
        )

    verify_run_scope(gateway, property_id)
    return property_id


def verify_run_scope(gateway, property_id: str | None) -> None:
    """The run's property, the asker's authority, and the out-of-band approval.

    Every call here is refused by the request interceptor before any target Lambda
    runs, so none of them can read or change anything. They prove the deployed
    Gateway enforces what ``tests/unit/test_run_scope.py`` proves offline.
    """
    import run_context

    print("\n  -- run scope, enforced outside the model --\n")
    if property_id is None:
        check("a property to scope against was discovered", False, "list_properties failed")
        return
    today = datetime.date.today().isoformat()

    def attempt(label: str, target: str, tool: str, arguments: dict, **context) -> str | None:
        run_context.set_current(
            run_context.RunContext(
                property_id=property_id,
                operating_date=today,
                trigger="chat",
                # A chat run always has an asker. A Manager unless the check says
                # otherwise, so each check fails on the rule it is about.
                **{"caller_groups": ("Manager",), **context},
            )
        )
        # Headers are fixed when the client is built, so each context gets its own.
        client = gateway._client_for(target, gateway.correlation_headers(target))
        with client:
            result = client.call_tool_sync(
                tool_use_id=f"verify-scope-{label}",
                name=f"{target}{DELIMITER}{tool}",
                arguments=arguments,
            )
        return (envelope_of(result).get("error") or {}).get("code")

    code = attempt("other-hotel", "billing", "list_folios", {"propertyId": ANOTHER_HOTEL})
    check(
        "a run about one hotel cannot read another's folios by naming it",
        code == "OUT_OF_SCOPE",
        f"code={code}",
    )

    code = attempt(
        "no-groups", "billing", "list_folios", {"propertyId": property_id},
        caller_groups=(),
    )
    check(
        "an asker in no staff group can call nothing -- never the agents' authority",
        code == "NOT_PERMITTED_FOR_CALLER",
        f"code={code}",
    )

    code = attempt(
        "front-desk",
        "arrivals",
        "assign_room",
        {"propertyId": property_id, "reservationId": NO_SUCH_ID, "roomId": NO_SUCH_ID,
         "reason": "layer 2 probe"},
        caller_groups=("FrontDesk",),
    )
    check(
        "a front-desk operator's copilot cannot pre-assign a room, which the "
        "platform reserves for Managers",
        code == "NOT_PERMITTED_FOR_CALLER",
        f"code={code}",
    )

    code = attempt(
        "forged-header",
        "billing",
        "post_charge",
        {"propertyId": property_id, "folioId": NO_SUCH_ID, "description": "layer 2 probe",
         "amount": 1},
        approval_token="apv-forged-not-a-real-approval-0000000",  # nosec B106 - deliberately forged
    )
    check(
        "a forged approval arriving the way a real one does is still refused "
        "(fails closed, never open)",
        code in ("APPROVAL_INVALID", "APPROVAL_UNVERIFIABLE"),
        f"code={code}",
    )


# --------------------------------------------------------------------------- #
# 4: routing
# --------------------------------------------------------------------------- #


def verify_routing(property_id: str | None) -> None:
    """The orchestrator must pick the specialist by domain, not at random."""
    print("\n  -- orchestrator routing (invokes the model) --\n")
    import orchestrator
    import run_context

    context = run_context.RunContext(
        property_id=property_id,
        operating_date=datetime.date.today().isoformat(),
        trigger="chat",
        caller_groups=("Manager",),
    )
    run_context.set_current(context)

    agent = orchestrator.build(context)
    question = (
        f"Which rooms should today's arrivals at property {property_id} be "
        "assigned to, and why?"
    )
    try:
        result = agent(question)
    except Exception as exc:  # noqa: BLE001
        check("the orchestrator answers a room-assignment question", False,
              f"{type(exc).__name__}: {exc}")
        return

    delegated = [
        block["toolUse"]["name"]
        for message in agent.messages
        for block in (message.get("content") or [])
        if isinstance(block, dict) and "toolUse" in block
    ]
    # The answer, not just the verdict: an orchestrator that declines to delegate
    # usually says why -- it asked a clarifying question, or refused for want of a
    # property -- and that sentence is the whole diagnosis.
    answer = " ".join(str(result).split())[:400] or "(no text)"
    check(
        "a room-assignment question is routed to arrivals_agent",
        "arrivals_agent" in delegated,
        f"delegated to {delegated or 'nothing'}\n        answered: {answer}",
    )
    check(
        "and not to billing_agent, which has no business in that decision",
        "billing_agent" not in delegated,
        f"delegated to {delegated or 'nothing'}",
    )


# --------------------------------------------------------------------------- #


def main() -> int:
    with_model = "--no-model" not in sys.argv
    load_environment()
    # After the environment is populated: config.py reads it at call time, but
    # importing the agent modules before AWS_REGION is set would resolve clients
    # against the ambient us-west-2.
    sys.path.insert(0, str(REPO_ROOT / "agents"))
    import gateway
    import run_context

    # A run context so the decision-log interceptor sees real correlation headers
    # rather than recording every one of these calls as unattributed.
    run_context.set_current(
        run_context.RunContext(
            property_id=None,
            operating_date=datetime.date.today().isoformat(),
            trigger="chat",
            caller_groups=("Manager",),
        )
    )

    property_id = verify_tool_plane(gateway)
    if with_model:
        verify_routing(property_id)
    else:
        print("\n  (skipping the routing check: --no-model)\n")

    failed = [label for ok, label in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for label in failed:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
