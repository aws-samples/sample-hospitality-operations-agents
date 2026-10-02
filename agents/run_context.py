# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-invocation context, carried out of band of the model's conversation.

A Strands ``@tool`` function receives only the string the model wrote. But the
sub-agents need facts the model must not be free to invent or alter: which
property this run is about, which operating date, and the run id every decision
gets logged under.

Putting those in a ``ContextVar`` rather than in the delegated prompt means the
orchestrator cannot accidentally hand A2 the wrong property by paraphrasing, and
the model cannot widen its own scope by writing a different propertyId into its
delegation text. That last guarantee is *enforced*, not merely intended: the context
reaches the Gateway as ``X-Hotel-Ops-*`` headers (``gateway.correlation_headers``),
which the model has no channel to, and the Gateway's request interceptor refuses any
tool call whose ``propertyId`` differs from the run's. An earlier version of this
docstring claimed the guarantee while the only thing carrying the property to the
tools was prompt text. ``ContextVar`` is the right primitive here specifically because
the entrypoint is async: each invocation's context is isolated even while
several are in flight in one warm container.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from config import default_property_id


@dataclass(frozen=True)
class RunContext:
    """What one invocation is about."""

    #: The property under discussion. ``None`` means chain-wide, which only A5
    #: (and A1/A3/A4's cross-property reads) can actually serve -- A2's identity
    #: is resolved per property, so it will refuse.
    property_id: str | None

    #: The hotel operating date. Not necessarily today: a night audit run just
    #: after midnight is reasoning about the day that just ended, and the
    #: foundation's reporting endpoints are keyed on the date, not on "now".
    operating_date: str

    #: Correlates every decision this run produces in the decision log.
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    #: Where the invocation came from: "chat" (a human in the ops console),
    #: "schedule", or "event". A4 and A5 advise either way, but a scheduled A1
    #: run should not ask a clarifying question that nobody will read.
    trigger: str = "chat"

    #: Cognito groups of the human on whose behalf this runs, when there is one.
    #: The copilot path forwards the operator's own ID token to the Runtime, so
    #: this is the operator's real authority, not the agent's.
    caller_groups: tuple[str, ...] = ()

    #: The approval a human released, present only on the execution run that
    #: release starts. Carried here and sent to the Gateway as a header, never put in
    #: a prompt: text the model reads is text AgentCore Memory stores and traces
    #: record, and on the first real approval the model repeated the token in its
    #: answer. ``repr=False`` so a logged context cannot leak it either.
    approval_token: str | None = field(default=None, repr=False)

    @property
    def scope_label(self) -> str:
        """Stable, filesystem-safe scope key used in memory actor ids."""
        return self.property_id or "_chain"

    @property
    def memory_scope(self) -> str:
        """The scope as AgentCore Memory will accept it.

        Not :attr:`scope_label`: Memory's ``sessionId`` must start with a letter or
        digit, and ``_chain`` starts with an underscore.
        """
        return self.property_id or "chain"

    @property
    def session_id(self) -> str:
        """One Memory session per *run*, never shared between runs.

        It used to be ``{property}:{operating_date}``, deliberately shared, so that a
        night-audit run could recall the evening's arrivals. A security review found
        what that also meant: every chat, every approval execution and every scheduled
        run at a property that day reloaded one history, so a front-desk user could
        plant text that a Manager's run, or an unattended one, later read as context --
        and chat callers choose ``operatingDate``, so they could pick the session.

        The run id makes the session private to its run. Cross-run recall now comes
        only from long-term memory, which only unattended runs read or write (see
        ``memory.build_session_manager``).

        The format is also now one the service accepts. Its ``sessionId`` pattern is
        ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` -- no colons -- and the old value had one; with
        ``#`` in the old actor id as well, every memory write was most likely rejected,
        which would explain why the memory store held no events at all.
        """
        raw = f"{self.memory_scope}-{self.operating_date}-{self.run_id}"
        cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", raw)
        return cleaned[:MAX_MEMORY_SESSION_ID]

    def actor_id(self, agent: str) -> str:
        """Memory actor: one per agent per property.

        Deliberately *not* the bare agent name. Durable facts are
        property-specific -- "floor 4 west takes longer to turn over" is true of
        one hotel, and letting it surface while reasoning about another property
        would be a tenancy leak dressed up as a feature.

        Separated by ``/``, which Memory's ``actorId`` pattern allows; the ``#`` this
        used to use is not in that pattern.
        """
        return f"{agent}/{self.memory_scope}"


#: AgentCore Memory's ``sessionId`` upper bound.
MAX_MEMORY_SESSION_ID = 100

_current: ContextVar[RunContext | None] = ContextVar("hotel_ops_run_context", default=None)


def from_payload(payload: dict | None) -> RunContext:
    """Build a context from an invocation payload, applying deployment defaults."""
    payload = payload if isinstance(payload, dict) else {}
    property_id = (payload.get("propertyId") or default_property_id() or "").strip()
    operating_date = (payload.get("operatingDate") or "").strip() or _today()

    _validate_date(operating_date)

    groups = payload.get("callerGroups") or []
    return RunContext(
        property_id=property_id or None,
        operating_date=operating_date,
        run_id=(payload.get("runId") or "").strip() or str(uuid.uuid4()),
        trigger=(payload.get("trigger") or "chat").strip() or "chat",
        caller_groups=tuple(g for g in groups if isinstance(g, str)),
        approval_token=(payload.get("approvalToken") or "").strip() or None,
    )


def preamble(request: str, context: RunContext, *, audience: str) -> str:
    """Prepend the run's facts to a request as a user-turn preamble.

    Both the orchestrator and every sub-agent get one. That is not symmetry for its
    own sake -- it is a bug fix. The orchestrator originally got none, on the
    assumption that whoever invoked it had put the property in the prompt. An
    interactive caller does. A schedule does not: it sends a standing instruction
    ("pre-assign rooms for the unassigned arrivals at this property") and supplies
    the property out of band. So the first scheduled run asked a human which
    property and which date it should use, on a trigger where no human is reading,
    and delegated to nobody. Its own system prompt promised it "the run context
    below"; there was no below.

    Deliberately *not* appended to the system prompt. The system prompt is long,
    static, and cached by Bedrock; splicing per-run values into it would change the
    cached prefix on every invocation and throw away the cache. As a user turn it
    costs a few dozen uncached tokens instead.

    It also puts these facts in the same place as the request itself, which is the
    honest framing: the caller's text and the run scope arrive together, and where
    they conflict, the scope is the one that came from the invoking system rather
    than from a model.

    Args:
        audience: ``"orchestrator"`` or ``"specialist"``. The facts are identical;
            what differs is what to do with them. A specialist passes ``propertyId``
            to tools. The orchestrator has no such tools -- its tools are the
            specialists -- and its job is to name the property in the text it
            delegates, because a specialist that has to guess is a specialist that
            asks a question instead of working.
    """
    lines = [
        "<run_context>",
        f"runId: {context.run_id}",
        f"propertyId: {context.property_id or '(none — chain-wide)'}",
        f"operatingDate: {context.operating_date}",
        f"trigger: {context.trigger}",
    ]
    if context.caller_groups:
        lines.append(f"callerGroups: {', '.join(context.caller_groups)}")

    lines.extend(["", "These are facts about this run, supplied by the invoking system."])

    if audience == "orchestrator":
        lines.extend(
            [
                "The request below may not name the property or the date, because a",
                "scheduled or reactive trigger supplies them here instead. Take them",
                "from this block and state propertyId explicitly in every delegation.",
                "Never ask which property or which date: you have both.",
            ]
        )
    else:
        lines.extend(
            [
                "Use propertyId for every tool call that needs one.",
                "operatingDate is what 'today' means for this run. Pass it where a tool",
                "asks for a date, but do not narrow a tool's own default window down to",
                "it: a tool that looks ahead by default looks ahead on purpose, and",
                "collapsing it to a single day throws away the work you were asked to do.",
                "If the request below names a different property, do not act on it: say",
                "that the request and the run scope disagree.",
            ]
        )

    lines.extend(
        [
            "When trigger is not 'chat', no human is reading in real time — do not",
            "ask questions, and state your assumptions in your answer instead.",
            "</run_context>",
            "",
            request,
        ]
    )
    return "\n".join(lines)


def set_current(context: RunContext) -> None:
    _current.set(context)


def current() -> RunContext:
    context = _current.get()
    if context is None:
        # Reaching a sub-agent with no context means main.py did not set one,
        # which would silently produce runs with no property and no run id.
        raise RuntimeError(
            "No RunContext is set for this invocation. main.py must call "
            "set_current() before delegating to any sub-agent."
        )
    return context


def _today() -> str:
    # UTC, because the foundation's reporting endpoints are UTC-dated and a
    # local-time default would silently query the wrong day for half the chain.
    return datetime.now(timezone.utc).date().isoformat()


def _validate_date(value: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"operatingDate must be YYYY-MM-DD, got {value!r}"
        ) from exc
