# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator: one agent whose only tools are the five specialists.

Agents-as-tools, deliberately, over Strands' swarm and graph primitives. The
architecture this system is built to depends on one property that neither of
those preserves: **a sub-agent never talks to another sub-agent.** Swarm and
graph both allow peer-to-peer handoff, which would let the housekeeping agent
hand work to the billing agent with no human and no orchestrator in the path.
Agents-as-tools makes that structurally impossible -- a sub-agent's tool list
contains only its own Gateway target's tools, so there is no edge to traverse.
Workflow would preserve the isolation but give up model-driven routing, which is
the one thing worth having here.

The orchestrator holds no foundation tools of its own. It cannot read a folio or
assign a room; it can only ask a specialist to. That is what makes the tool plane
auditable: every foundation call in this system originates from exactly one of
five named agents, each signing in as its own Cognito identity.
"""

from __future__ import annotations

import logging

from strands import Agent

from memory import build_session_manager
from model import build_model
from prompt_loader import load
from run_context import RunContext
from subagents.arrivals import arrivals_agent
from subagents.billing import billing_agent
from subagents.housekeeping import housekeeping_agent
from subagents.night_audit import night_audit_agent
from subagents.regional import regional_agent

logger = logging.getLogger(__name__)

KEY = "orchestrator"

#: Order is stable and not alphabetical: it runs A1 through A5, matching the
#: prompt's table and the design docs, so a reader comparing the two does not
#: have to re-map them.
SPECIALISTS = (
    arrivals_agent,
    housekeeping_agent,
    billing_agent,
    night_audit_agent,
    regional_agent,
)


def build(context: RunContext) -> Agent:
    """Construct the orchestrator for one invocation.

    Blocking: the session manager reads this session's history from AgentCore
    Memory during construction. Strands does not allow async callbacks for
    ``AgentInitializedEvent``, so this cannot be avoided -- call it from a worker
    thread if the caller is on an event loop.
    """
    agent = Agent(
        model=build_model(),
        system_prompt=load(KEY),
        tools=list(SPECIALISTS),
        agent_id=KEY,
        name=KEY,
        # True because the entrypoint streams. The per-turn memory calls are
        # offloaded off the event loop, which keeps the SSE stream flowing while
        # a delegation is in flight.
        session_manager=build_session_manager(KEY, context, async_mode=True),
        # Streaming is the interface; the default handler would print the same
        # tokens to the container log on top of yielding them.
        callback_handler=None,
    )
    logger.info(
        "orchestrator ready run=%s property=%s date=%s trigger=%s",
        context.run_id,
        context.scope_label,
        context.operating_date,
        context.trigger,
    )
    return agent
