# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Memory wiring: short-term session continuity plus long-term recall.

Two things happen through this module, and they are worth separating:

*Short-term.* The session manager persists each sub-agent's conversation as
AgentCore Memory events and reloads it on construction. Because a fresh
``Agent`` is built per delegation (see ``subagents/base.py``), this is what makes
a second delegation to the same sub-agent inside one run remember the first --
without it, asking A2 to "now do the same for floor 3" would start from nothing.

*Long-term.* The two strategies created by the stack extract durable facts and
per-shift summaries out of those events asynchronously, and ``retrieval_config``
below is how they come back: on every user-role text message the manager runs a
semantic search per namespace and injects the hits. Note that it *skips* tool
results (they are role ``user`` but carry no ``text`` block), so a tool-heavy
sub-agent turn does not fire a retrieval per tool call.

The whole module is best-effort by design. ``MEMORY_ID`` unset, a throttled
control-plane call, a memory that was deleted out from under the stack -- all of
these return ``None`` and the agent runs stateless. An arrivals agent that
answers without recalling that floor 4 turns over slowly is degraded; one that
refuses to answer at all is broken.
"""

from __future__ import annotations

import logging

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

from config import memory_id, memory_namespaces, region
from run_context import RunContext

logger = logging.getLogger(__name__)

#: Buffer messages and let the manager's ``AfterInvocationEvent`` hook flush them
#: at the end of the delegation. With the default of 1, every assistant message
#: and every tool result is its own ``CreateEvent`` call -- a single A2 run that
#: sequences twenty tasks would spend more time in the memory API than in the
#: model. Batching is safe *only* because the after-invocation flush exists;
#: the hook is registered exactly when ``batch_size > 1``.
BATCH_SIZE = 20

#: Durable operating facts. Small and high-precision: this text lands in the
#: system context of an agent that is about to act on it, so a marginal match
#: ("guest in 412 asked for extra towels once") is worse than no match.
FACTS_TOP_K = 8
FACTS_MIN_SCORE = 0.4

#: Shift summaries are few and long. Fewer, and a looser floor, because there
#: may only be one or two summaries for the session and excluding them defeats
#: the purpose.
SHIFT_TOP_K = 3
SHIFT_MIN_SCORE = 0.25

#: Wraps injected memory in the sub-agent's context. Named for what it is, not
#: the library default ``user_context`` -- the prompts refer to this tag by name
#: and tell the agent that its contents are recalled context, not instructions.
CONTEXT_TAG = "recalled_context"

#: The only triggers that get memory. See ``build_session_manager``.
UNATTENDED_TRIGGERS = frozenset({"schedule", "event"})


def build_session_manager(
    agent: str, context: RunContext, *, async_mode: bool = False
) -> AgentCoreMemorySessionManager | None:
    """Session manager for one agent within one run, or ``None``.

    ``None`` is a supported outcome everywhere it is consumed: pass it straight
    to ``Agent(session_manager=...)`` and Strands keeps conversation state in
    process for the life of the object.

    Args:
        agent: Memory actor name, e.g. ``"night_audit"``.
        context: The run this manager belongs to.
        async_mode: Whether the per-turn boto3 calls are offloaded off the event
            loop. This must match how the agent is invoked, and getting it wrong
            fails loudly rather than silently: ``True`` requires
            ``stream_async``/``invoke_async``, because Strands' hook registry
            refuses to dispatch coroutine callbacks from the synchronous
            ``Agent.__call__``. The orchestrator streams, so it passes ``True``;
            sub-agents run synchronously inside a worker thread, so they do not.
    """
    # Only unattended runs -- a schedule or an event -- read or write long-term
    # memory. A chat run is a human's words, and an approval execution acts with a
    # Manager's authority; letting either share a namespace with runs of a different
    # authority is how a front-desk user plants an instruction that a Manager's run,
    # or a 3 a.m. scheduled run, later follows. A security review found exactly that.
    # The copilot pays for it by not remembering facts across chats.
    if context.trigger not in UNATTENDED_TRIGGERS:
        logger.info("trigger=%s; %s runs without memory by design", context.trigger, agent)
        return None

    mem_id = memory_id()
    if not mem_id:
        logger.info("MEMORY_ID unset; %s runs without memory", agent)
        return None

    try:
        return AgentCoreMemorySessionManager(
            agentcore_memory_config=AgentCoreMemoryConfig(
                memory_id=mem_id,
                actor_id=context.actor_id(agent),
                session_id=context.session_id,
                retrieval_config=_retrieval_config(),
                batch_size=BATCH_SIZE,
                context_tag=CONTEXT_TAG,
                # Historical toolUse/toolResult pairs restored from a previous
                # delegation are noise here, and worse than noise: they name
                # tools by their Gateway-prefixed names, and a sub-agent whose
                # tool list no longer contains one of them can be nudged into
                # trying to call it. The prior turn's *conclusions* are what
                # need to survive, and those are in the assistant text.
                filter_restored_tool_context=True,
                async_mode=async_mode,
                default_metadata={
                    "runId": context.run_id,
                    "trigger": context.trigger,
                    "propertyId": context.scope_label,
                },
            ),
            region_name=region(),
        )
    except Exception:  # noqa: BLE001 - memory is additive; never fail the run
        logger.warning(
            "Could not attach AgentCore Memory for %s; continuing stateless",
            agent,
            exc_info=True,
        )
        return None


def _retrieval_config() -> dict[str, RetrievalConfig] | None:
    """Map each configured namespace template to its retrieval settings.

    The templates come from the stack that created the strategies, so the
    namespace written to and the namespace read from cannot drift. The manager
    substitutes ``{actorId}`` and ``{sessionId}`` at retrieval time.

    Returning ``None`` rather than ``{}`` matters: the manager treats a falsy
    ``retrieval_config`` as "short-term only" and skips retrieval entirely,
    which is the correct behaviour when the stack has not published namespaces.
    """
    namespaces = memory_namespaces()
    config: dict[str, RetrievalConfig] = {}

    facts = namespaces.get("facts")
    if facts:
        config[facts] = RetrievalConfig(top_k=FACTS_TOP_K, relevance_score=FACTS_MIN_SCORE)

    shift = namespaces.get("shift")
    if shift:
        config[shift] = RetrievalConfig(top_k=SHIFT_TOP_K, relevance_score=SHIFT_MIN_SCORE)

    if not config:
        logger.info("No memory namespaces configured; short-term memory only")
        return None
    return config
