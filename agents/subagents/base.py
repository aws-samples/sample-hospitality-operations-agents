# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shared machinery behind all five sub-agents.

Every sub-agent is the same three things — a system prompt, one Gateway target's
tools, and a memory actor — so they are built by one function and differ only in
those three arguments. The per-agent modules exist to hold the ``@tool``
docstring the orchestrator routes on, not to hold logic.

Two decisions here are worth reading before changing anything.

**Delegations run in a worker thread.** The Runtime entrypoint is async and
streams, and everything inside a delegation is synchronous: ``MCPClient`` is a
sync context manager, ``Agent(...)`` construction does blocking reads against
AgentCore Memory, and ``Agent.__call__`` blocks for the length of a multi-turn
tool loop. Running that on the event loop would stall the SSE stream for tens of
seconds. ``asyncio.to_thread`` keeps the loop free, and it copies the current
``contextvars`` context, so ``run_context.current()`` still resolves inside the
thread. It also lets the memory session manager stay in synchronous-hook mode,
which is required because ``Agent.__call__`` refuses to dispatch async hooks.

**A fresh Agent per delegation.** Not a module-level singleton. The MCP session
is only valid inside its ``with`` block, so the agent that uses those tools must
live and die inside it. This costs one ``tools/list`` round trip, and buys three
things: a connection whose state is knowable, tool lists that cannot outlive
their session, and — because the session manager reloads history on construction
— a second delegation to the same sub-agent in one run that remembers the first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Sequence

from strands import Agent
from strands.types.tools import AgentTool

from gateway import tools_for
from memory import build_session_manager
from model import build_model
from prompt_loader import load
from run_context import current, preamble

logger = logging.getLogger(__name__)

#: Sub-agents that operate on one property cannot be given a chain-wide scope.
#: The housekeeping tools all require ``propertyId``, and the foundation resolves
#: a per-property Cognito identity from it, so there is nothing sensible to
#: guess. Refuse in words the orchestrator can act on rather than calling a tool
#: with an empty string and surfacing a 400.
PROPERTY_REQUIRED = frozenset({"housekeeping"})


async def delegate(
    *,
    key: str,
    target: str,
    request: str,
    extra_tools: Callable[[], Sequence[AgentTool]] | None = None,
) -> str:
    """Run one sub-agent to completion and return its answer as text.

    Args:
        key: Prompt and memory-actor name, e.g. ``"night_audit"``.
        target: Gateway target whose tools this agent gets, e.g. ``"nightaudit"``.
        request: What the orchestrator wants done, in its own words.
        extra_tools: Built lazily, inside the worker thread, for tools that open
            their own remote session (Code Interpreter) and so must not be
            constructed at import time.
    """
    return await asyncio.to_thread(_run, key, target, request, extra_tools)


def _run(
    key: str,
    target: str,
    request: str,
    extra_tools: Callable[[], Sequence[AgentTool]] | None,
) -> str:
    context = current()

    if key in PROPERTY_REQUIRED and not context.property_id:
        raise ValueError(
            f"The {key} agent works on exactly one property and this run has no "
            f"property scope. Re-invoke with propertyId in the payload, or ask a "
            f"chain-level agent instead."
        )

    with tools_for(target, agent=key) as mcp_tools:
        tools: list[AgentTool] = list(mcp_tools)
        if extra_tools is not None:
            tools.extend(extra_tools())

        agent = Agent(
            model=build_model(),
            system_prompt=load(key),
            tools=tools,
            agent_id=key,
            name=key,
            session_manager=build_session_manager(key, context),
            # The Runtime's stdout is the container log. The default handler
            # prints every token, which would duplicate the whole conversation
            # into CloudWatch at a cost, and tell us nothing the traces do not.
            callback_handler=None,
        )

        logger.info(
            "delegating run=%s agent=%s property=%s tools=%d",
            context.run_id,
            key,
            context.scope_label,
            len(tools),
        )
        return str(agent(preamble(request, context, audience="specialist")))
