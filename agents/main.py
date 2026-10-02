# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Runtime entrypoint for the hotel operations agent graph.

``BedrockAgentCoreApp`` supplies the two HTTP contracts the Runtime requires --
``POST /invocations`` and ``GET /ping`` on port 8080 -- so this module only has to
supply the handler. Because the handler is an async generator, the app returns a
``text/event-stream`` and each yielded dict becomes one ``data:`` frame.

The events yielded here are a curated vocabulary, not Strands' raw stream. Two
reasons. The raw events carry full tool inputs, and a Tier-2 billing call's
arguments include the approval token a human just issued -- that has no business
being echoed to a browser. And a stable event shape means the ops console does
not break when a Strands version changes the internals of a chunk.

Invocation payload::

    {
      "prompt":        "Assign rooms for today's arrivals",   # required
      "propertyId":    "uuid",            # optional; falls back to PROPERTY_SCOPE
      "operatingDate": "2026-09-08",      # optional; defaults to today, UTC
      "trigger":       "chat|schedule|event",   # optional; defaults to chat
      "runId":         "uuid",            # optional; generated when absent
      "callerGroups":  ["Manager"]        # optional; the human's Cognito groups
    }
"""

from __future__ import annotations

import asyncio
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import orchestrator
import run_context
from config import log_level

app = BedrockAgentCoreApp()

logging.basicConfig(level=log_level(), format="%(levelname)s %(name)s %(message)s")
# These two are chatty at INFO and describe HTTP plumbing, not operations. The
# agent's own log lines are the ones worth paying CloudWatch for.
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("hotel_ops.main")

#: Accepted spellings of the prompt field, in precedence order. Chat clients,
#: the ``agentcore invoke`` CLI, and the scheduled invoker Lambda have each
#: settled on a different one, and rejecting two of the three would be a support
#: burden with no upside.
PROMPT_KEYS = ("prompt", "message", "input", "text")


@app.entrypoint
async def invoke(payload, context):
    """Stream the orchestrator's answer for one request."""
    try:
        prompt = _prompt_from(payload)
        ctx = run_context.from_payload(payload)
    except ValueError as exc:
        # A malformed payload is the caller's bug, and saying so is more useful
        # than a stack trace in CloudWatch that the caller never sees.
        yield {"type": "error", "code": "INVALID_PAYLOAD", "message": str(exc)}
        return

    run_context.set_current(ctx)
    logger.info(
        "invocation run=%s agentcoreSession=%s property=%s date=%s trigger=%s",
        ctx.run_id,
        getattr(context, "session_id", None),
        ctx.scope_label,
        ctx.operating_date,
        ctx.trigger,
    )

    yield {
        "type": "run_started",
        "runId": ctx.run_id,
        "propertyId": ctx.property_id,
        "operatingDate": ctx.operating_date,
        "trigger": ctx.trigger,
    }

    # Constructing the orchestrator reads this session's history from AgentCore
    # Memory synchronously -- Strands does not permit async initialization hooks
    # -- so it goes to a worker thread rather than stalling the stream before the
    # first token.
    agent = await asyncio.to_thread(orchestrator.build, ctx)

    delegations: list[str] = []
    seen_tool_uses: set[str] = set()
    text_parts: list[str] = []

    # The orchestrator gets the run's facts prepended, exactly as the sub-agents do.
    # Without this a scheduled trigger -- which sends a standing instruction and
    # supplies the property out of band -- produces an orchestrator that asks which
    # property it should use, on a trigger where nobody is reading, and delegates to
    # nobody. Its own system prompt refers to "the run context below"; this is it.
    async for event in agent.stream_async(
        run_context.preamble(prompt, ctx, audience="orchestrator")
    ):
        if delta := event.get("data"):
            text_parts.append(delta)
            yield {"type": "text", "delta": delta}

        if reasoning := event.get("reasoningText"):
            yield {"type": "reasoning", "delta": reasoning}

        # current_tool_use repeats for every input fragment as the model streams
        # the tool call, so dedupe on toolUseId to emit one delegation event.
        tool_use = event.get("current_tool_use") or {}
        tool_use_id = tool_use.get("toolUseId")
        name = tool_use.get("name")
        if tool_use_id and name and tool_use_id not in seen_tool_uses:
            seen_tool_uses.add(tool_use_id)
            delegations.append(name)
            # The name only -- never the input. See the module docstring.
            yield {"type": "delegation", "agent": name}

        if (result := event.get("result")) is not None:
            yield {
                "type": "run_completed",
                "runId": ctx.run_id,
                "stopReason": getattr(result, "stop_reason", None),
                "delegations": delegations,
                "text": "".join(text_parts),
                "usage": _usage_of(result),
            }


def _prompt_from(payload) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Invocation payload must be a JSON object.")

    for key in PROMPT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise ValueError(
        f"No prompt in the invocation payload. Provide one of: {', '.join(PROMPT_KEYS)}."
    )


def _usage_of(result) -> dict | None:
    """Token usage for the orchestrator turn, when the metrics carry it.

    Best-effort by intent: this is cost telemetry, and a Strands version that
    reshapes ``EventLoopMetrics`` must not be able to fail an invocation that has
    already produced a correct answer.
    """
    try:
        usage = result.metrics.accumulated_usage
        return {
            "inputTokens": usage.get("inputTokens"),
            "outputTokens": usage.get("outputTokens"),
            "totalTokens": usage.get("totalTokens"),
            "cacheReadInputTokens": usage.get("cacheReadInputTokens"),
            "cacheWriteInputTokens": usage.get("cacheWriteInputTokens"),
        }
    except Exception:  # noqa: BLE001 - telemetry must never break the response
        logger.debug("Could not read usage metrics", exc_info=True)
        return None


if __name__ == "__main__":
    # Guarded so the unit tests can import this module and call invoke() without
    # starting a server. The Runtime entrypoint runs this file as a script, and
    # `python agents/main.py` locally does the same.
    app.run()
