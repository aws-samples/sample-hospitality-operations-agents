#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app for the Hotel Operations Agent.

Every stack is named ``hotel-ops-agent-*`` and is strictly additive to the
hospitality foundation (``anycompany-booking``). The foundation is read at synth
time and never modified: the only resources this app creates inside it are new
Cognito *users* and, later, new EventBridge *rules* on the existing bus.
"""

from __future__ import annotations

import os
import re
import sys

import aws_cdk as cdk

sys.path.insert(0, os.path.dirname(__file__))

from stacks.agentcore_stack import AgentCoreStack  # noqa: E402
from stacks.evaluation_stack import EvaluationStack  # noqa: E402
from stacks.foundation_config import FoundationConfig  # noqa: E402
from stacks.frontend_stack import FrontendStack  # noqa: E402
from stacks.identity_stack import IdentityStack  # noqa: E402
from stacks.api_stack import ApiStack  # noqa: E402
from stacks.orchestration_stack import OrchestrationStack  # noqa: E402
from stacks.tools_stack import ToolsStack  # noqa: E402

app = cdk.App()


def ctx(key: str, default: str) -> str:
    return app.node.try_get_context(key) or default


region = os.environ.get("CDK_DEFAULT_REGION") or "us-east-1"
environment = ctx("environment", "dev")

# Read-only resolution of the prerequisite. Fails the synth loudly if the
# foundation is absent or partially deployed -- a silent default would produce a
# stack that deploys cleanly and then 403s on every call at runtime.
foundation = FoundationConfig.resolve(
    stack_name=ctx("foundationStackName", "anycompany-booking"),
    region=region,
    environment=environment,
)

# The platform lets a signed-in user rewrite their own custom:property_id and
# custom:region (its SPA app client lists both in WriteAttributes). A security review
# found that, and this project resolves scope server-side instead: the console takes
# every operator's scope from hotel-ops-agent-staff-scope, written only with IAM
# credentials by scripts/register_staff_scope.py, and refuses a token whose claims
# disagree. So this is a warning, not a refusal -- but it is still worth fixing in the
# platform, whose own authorizer trusts those claims for its own UI.
#
# Skipped for a JSON-configured foundation (CI and offline synth), where there is no
# user pool to ask.
if not os.environ.get("HOTEL_OPS_FOUNDATION_JSON"):
    from stacks.foundation_config import writable_scope_attributes

    _writable = writable_scope_attributes(foundation, region=foundation.region)
    if _writable:
        print(
            f"NOTE: the platform's console app client lets users rewrite their own "
            f"{', '.join(_writable)}. This project does not trust those claims -- scope "
            "comes from hotel-ops-agent-staff-scope (scripts/register_staff_scope.py) -- "
            "but the platform's own authorizer does. See IMPLEMENTATION_GUIDE.md §1.",
            file=sys.stderr,
        )

env = cdk.Environment(account=foundation.account or None, region=foundation.region)

identity = IdentityStack(
    app,
    "hotel-ops-agent-identity",
    foundation=foundation,
    email_domain=ctx("agentEmailDomain", "anycompany.internal"),
    salt=ctx("identitySalt", "1"),
    env=env,
    description=(
        "Hotel Operations Agent: per-agent Cognito identities and credentials "
        "(additive to the anycompany-booking user pool)"
    ),
)

tools = ToolsStack(
    app,
    "hotel-ops-agent-tools",
    foundation=foundation,
    identity=identity,
    api_pacing_ms=int(ctx("apiPacingMs", "50")),
    env=env,
    description=(
        "Hotel Operations Agent: the five AgentCore Gateway target Lambdas, one "
        "per agent domain, each authenticating as its own Cognito identity"
    ),
)
# Implied by the cross-stack secret references, but stated so the ordering
# survives a future refactor that stops referencing them: the Cognito users must
# exist before a function tries to sign in as one.
tools.add_stack_dependency(identity)

agentcore = AgentCoreStack(
    app,
    "hotel-ops-agent-agentcore",
    foundation=foundation,
    tools=tools,
    model_id=ctx("modelId", "us.anthropic.claude-sonnet-5"),
    # Unset means chain-wide: the invocation payload must then carry propertyId
    # for anything reaching A1-A4. Set it to pin a single-property deployment.
    property_scope=ctx("propertyScope", ""),
    # Gateway DEBUG exceptions echo the target's own error text to the client,
    # which is what lets a sub-agent reason about a real 409. Off outside dev.
    debug_gateway_exceptions=environment == "dev",
    env=env,
    description=(
        "Hotel Operations Agent: the AgentCore reasoning layer -- MCP Gateway "
        "with the Tier-2 approval interceptor and the decision log, Memory, a "
        "sandboxed Code Interpreter, and the Runtime hosting the orchestrator "
        "and all five sub-agents"
    ),
)
# The Gateway attaches the tool Lambdas by ARN, so the reference already orders
# these. Stated for the same reason as above: the ordering must not depend on a
# reference surviving a refactor.
agentcore.add_stack_dependency(tools)

# One pilot property by default, not all 50. A1 at 30 minutes and A2 at 20 is ~120
# agent runs per property per day; fanning that across the chain would be ~6,000
# daily runs writing to the foundation. Widen deliberately, with the cost in mind.
# No default, deliberately. A baked-in property uuid would be one from whichever
# account this was developed in: it would not exist in yours, and the schedules would
# deploy cleanly and then run against nothing. OrchestrationStack raises with an
# actionable message when this is empty.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
pilot_properties = tuple(
    p.strip() for p in ctx("pilotPropertyIds", "").split(",") if p.strip()
)

# Each must be a real property uuid. `-c pilotPropertyIds=all` was accepted once and
# deployed cleanly into something useless: the event rules filtered on
# `detail.propertyId = "all"`, which no platform event ever carries, so they could
# never fire; and the schedules would have run A1, A2 and A4 against a property that
# does not exist. There is no "all" -- A5 is the chain-wide agent, and it takes no
# property at all.
_malformed = [p for p in pilot_properties if not _UUID.fullmatch(p)]
if _malformed:
    raise SystemExit(
        f"pilotPropertyIds must be property uuids, got {', '.join(map(repr, _malformed))}. "
        "Pick one from the ops console's property picker, or from "
        "tests/integration/verify_tools.py, which prints one it can reach."
    )

orchestration = OrchestrationStack(
    app,
    "hotel-ops-agent-orchestration",
    foundation=foundation,
    agentcore=agentcore,
    pilot_property_ids=pilot_properties,
    # Off unless asked. Arming these starts unattended agent runs that write to the
    # foundation on a timer, which is a decision a deploy should not make on
    # someone's behalf -- see the stack's docstring.
    enable_triggers=ctx("enableTriggers", "false").lower() == "true",
    env=env,
    description=(
        "Hotel Operations Agent: unattended operation -- the decision log and "
        "approval tables the Gateway interceptors read, the queue-backed Runtime "
        "invoker, and the Scheduler cadences and foundation event rules that wake "
        "the agents"
    ),
)
# The invoker's environment carries the Runtime ARN, which already orders these.
orchestration.add_stack_dependency(agentcore)

api = ApiStack(
    app,
    "hotel-ops-agent-api",
    foundation=foundation,
    orchestration=orchestration,
    env=env,
    description=(
        "Hotel Operations Agent: the ops console's backend -- streaming-free chat "
        "onto the invocation queue, the Tier-2 approval queue, and the decision log "
        "read back for humans. Cognito-authorized against the foundation's existing "
        "user pool."
    ),
)
# The functions reference the tables and the chat queue directly, which orders these.
api.add_stack_dependency(orchestration)

evaluation = EvaluationStack(
    app,
    "hotel-ops-agent-evaluation",
    agentcore_stack=agentcore,
    # 10% by default. Every sampled session costs judge invocations on top of the run
    # itself; raise it to 100 while verifying and put it back afterwards.
    sampling_percent=int(ctx("evaluationSampling", "10")),
    env=env,
    description=(
        "Hotel Operations Agent: online evaluation -- four built-in trajectory "
        "graders plus custom judges for room-assignment quality and for whether the "
        "answer an operator reads is a truthful account of what the run did"
    ),
)
# The data source resolves the Runtime and its production endpoint.
evaluation.add_stack_dependency(agentcore)

# Skipped unless frontend/dist exists, so `cdk deploy` of the backend stacks does
# not require a Node toolchain or a built bundle. scripts/build_frontend.sh produces
# it; the stack itself refuses to synth against a missing one rather than deploying an
# empty bucket.
if (FRONTEND_DIST := os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")) and os.path.isfile(
    os.path.join(FRONTEND_DIST, "index.html")
):
    frontend = FrontendStack(
        app,
        "hotel-ops-agent-frontend",
        api=api,
        env=env,
        description=(
            "Hotel Operations Agent: the ops console -- its own S3 bucket and "
            "CloudFront distribution, with the console API as a second origin at "
            "/api/* so the whole thing is same-origin"
        ),
    )
    frontend.add_stack_dependency(api)

cdk.Tags.of(app).add("Project", "hotel-operations-agent")
cdk.Tags.of(app).add("Environment", environment)

app.synth()
