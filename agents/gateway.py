# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One MCP client per Gateway target, scoped to that target's tools.

The Gateway advertises all 28 tools at one endpoint, named
``{target}___{tool}`` -- three underscores, as the deployed Gateway confirms.
A sub-agent is given only the tools whose name starts with its own target
prefix, which is what makes ``hotel-operations-agent.md`` §3's isolation claim
literally true rather than aspirational: the housekeeping agent has no
``billing___post_charge`` in its tool list, so it cannot call it, cannot be
prompt-injected into calling it, and cannot hallucinate it into existence -- and
if it somehow named it anyway, the billing target's own Lambda would refuse
(verified in tests/integration/verify_tools.py).

Connections are opened per delegation rather than held open for the life of the
container. A warm AgentCore Runtime container can live for hours across many
sessions; a long-lived MCP session that silently dies leaves every subsequent
tool call failing for reasons no log line explains. One ``tools/list`` round trip
per delegation is a few tens of milliseconds against multi-second model latency,
and it buys a connection whose state is knowable.

Correlation headers
-------------------
Each session carries ``X-Hotel-Ops-*`` headers naming the run, the property, and
the delegating sub-agent. None of that is expressible in MCP: the protocol has a
tool name and arguments, and the Gateway's response interceptor -- which writes
the decision log -- otherwise has no way to know that thirty tool calls from four
sub-agents were one operator's request. They are plain unsigned headers, which
SigV4 permits (only ``Content-Type`` and the ``X-Amz-*`` set are signed), so no
change to :mod:`sigv4` is needed.

They also carry **authority**, which is why they are built here and nowhere else:
the run's property, the operator's groups, and -- on an execution run only -- the
approval a human released. The request interceptor enforces all three. Unsigned is
safe for that because of who can send them: the Gateway admits only SigV4 callers
holding ``InvokeGateway``, which is this Runtime's role, and inside the Runtime the
model's only channel to a request is a tool call's *arguments*. It can write any
``propertyId`` it likes into those; it cannot touch these.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterator, Sequence

from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_client import ToolFilters
from strands.types.tools import AgentTool

import run_context
from config import TARGETS, gateway_url, region
from sigv4 import SigV4Signer

logger = logging.getLogger(__name__)

#: Generous, because the first connection in a cold container also pays for
#: credential resolution. Still bounded, so a misconfigured Gateway URL fails
#: with a timeout rather than hanging the invocation.
STARTUP_TIMEOUT_SECONDS = 30


def _client_for(target: str, headers: dict[str, str]) -> MCPClient:
    if target not in TARGETS:
        raise ValueError(f"Unknown Gateway target {target!r}; expected one of {list(TARGETS)}")
    return MCPClient(
        url=gateway_url(),
        auth_provider=SigV4Signer(region=region()),
        headers=headers,
        # Anchored on the target prefix *plus* its delimiter, so "billing" cannot
        # also match a hypothetical "billing_admin" target added later. The
        # delimiter is matched as a run of two or more underscores rather than a
        # fixed count, so the filter does not silently pass everything -- or
        # nothing -- if the Gateway ever changes its width.
        tool_filters=ToolFilters(allowed=[re.compile(rf"^{re.escape(target)}_{{2,}}")]),
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        application_name="hotel-ops-agent",
    )


def correlation_headers(agent: str | None = None) -> dict[str, str]:
    """Run identity for the Gateway's response interceptor.

    Tolerates a missing run context on purpose. Every real delegation has one --
    :func:`subagents.base._run` resolves it before opening a session -- but this
    module is also the thing you reach for from a REPL to see what the Gateway is
    advertising, and refusing to connect for want of an audit header would make
    that harder for no safety gain. The interceptor records such a call as
    ``unattributed``, which is the truthful label for it.
    """
    try:
        context = run_context.current()
    except RuntimeError:
        logger.debug("no run context; Gateway calls will log as unattributed")
        return {"X-Hotel-Ops-Agent": agent} if agent else {}

    headers = {
        "X-Hotel-Ops-Run-Id": context.run_id,
        # scope_label rather than property_id: "_chain" is a real, queryable value
        # for a chain-wide run, where an omitted header would leave the decision
        # log's property index with a hole in it.
        "X-Hotel-Ops-Property-Id": context.scope_label,
        "X-Hotel-Ops-Operating-Date": context.operating_date,
        "X-Hotel-Ops-Trigger": context.trigger,
    }
    if agent:
        headers["X-Hotel-Ops-Agent"] = agent
    # The operator's own authority, when a human asked. The request interceptor
    # allows a tool in such a run only if the platform would let one of these groups
    # call the endpoint behind it -- which is what makes "the copilot inherits the
    # human's authority" true rather than a sentence in a prompt. Absent for a
    # scheduled or event run, where there is no human and the agent's own scoped
    # identity is the authority.
    # Always sent for a chat run, *even when empty*. An empty tuple used to omit the
    # header, and the interceptor reads an absent header as "no human asked" -- so a
    # human with no groups got the agents' full authority. Present-but-empty now
    # means "a human with no authority", and the interceptor refuses everything.
    if context.trigger == "chat" or context.caller_groups:
        headers["X-Hotel-Ops-Caller-Groups"] = ",".join(context.caller_groups)
    if context.approval_token:
        headers["X-Hotel-Ops-Approval-Token"] = context.approval_token
    return headers


@contextmanager
def tools_for(target: str, *, agent: str | None = None) -> Iterator[Sequence[AgentTool]]:
    """Open a scoped MCP session and yield only ``target``'s tools.

    The tools are only usable while the session is open, so the sub-agent must be
    constructed *and* run inside this block.

    Args:
        target: Gateway target name, e.g. ``"nightaudit"``.
        agent: The sub-agent doing the delegating, e.g. ``"night_audit"``. Passed
            explicitly rather than derived from ``target`` because the two spellings
            differ for A4, and a derived value would be wrong in the one row where
            it matters.
    """
    client = _client_for(target, correlation_headers(agent))
    with client:
        tools = _list_all(client)
        if not tools:
            # An empty list is never correct here: every target advertises at
            # least four tools. Silence would leave the sub-agent to answer from
            # the model's imagination, which is the one failure mode this whole
            # architecture exists to prevent.
            raise RuntimeError(
                f"Gateway returned no tools for target {target!r}. Either the "
                f"target is not attached to the Gateway, or the tool names it "
                f"advertises are not prefixed {target}___ as expected."
            )
        logger.info(
            "target=%s tools=%s", target, sorted(tool.tool_name for tool in tools)
        )
        yield tools


def _list_all(client: MCPClient) -> list[AgentTool]:
    """Drain every page. Filtering happens client-side, so a target's tools can
    land on any page and stopping at the first would truncate silently."""
    collected: list[AgentTool] = []
    token: str | None = None
    while True:
        page = client.list_tools_sync(pagination_token=token)
        collected.extend(page)
        token = getattr(page, "pagination_token", None)
        if not token:
            return collected
