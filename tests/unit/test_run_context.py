# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-invocation context: the payload contract and the run-context preamble.

The preamble tests exist because of a bug that reached a deployed schedule. The
orchestrator was never given the run's facts -- only sub-agents were -- on the
assumption that whoever invoked it had named the property in the prompt. An
interactive caller does. A schedule does not: it sends a standing instruction and
supplies the property out of band. So the first scheduled run answered "which
property and which date did you mean?" on a trigger where nobody was reading, and
delegated to nobody. These pin both audiences.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
import re
import sys
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "agents"


def _load():
    """Import ``agents/run_context.py``, which imports ``config``."""
    sys.path.insert(0, str(AGENTS))
    spec = importlib.util.spec_from_file_location(
        "hotel_run_context", AGENTS / "run_context.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and an unregistered module makes that None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_context = _load()


@pytest.fixture(autouse=True)
def no_deployment_default(monkeypatch):
    """PROPERTY_SCOPE is a deployment default. Unset unless a test sets it."""
    monkeypatch.delenv("PROPERTY_SCOPE", raising=False)


def context(**kw):
    return run_context.RunContext(
        **{
            "property_id": "p-1",
            "operating_date": "2026-09-08",
            "run_id": "run-1",
            "trigger": "chat",
            **kw,
        }
    )


# --------------------------------------------------------------------------- #
# from_payload
# --------------------------------------------------------------------------- #


def test_a_full_payload_is_taken_as_given():
    ctx = run_context.from_payload(
        {
            "propertyId": "p-9",
            "operatingDate": "2026-01-02",
            "runId": "r-9",
            "trigger": "event",
            "callerGroups": ["Manager", "FrontDesk"],
        }
    )
    assert (ctx.property_id, ctx.operating_date, ctx.run_id, ctx.trigger) == (
        "p-9",
        "2026-01-02",
        "r-9",
        "event",
    )
    assert ctx.caller_groups == ("Manager", "FrontDesk")


def test_an_absent_property_means_chain_wide_not_empty_string():
    """A2 checks `not context.property_id` to refuse. An empty string is falsy but
    would print as a property that does not exist."""
    assert run_context.from_payload({}).property_id is None
    assert run_context.from_payload({"propertyId": "   "}).property_id is None


def test_the_deployment_default_fills_in_a_missing_property(monkeypatch):
    monkeypatch.setenv("PROPERTY_SCOPE", "p-default")
    assert run_context.from_payload({}).property_id == "p-default"
    # But an explicit payload always wins over the deployment default.
    assert run_context.from_payload({"propertyId": "p-1"}).property_id == "p-1"


def test_the_trigger_defaults_to_chat_because_that_is_the_safe_assumption():
    """`chat` lets the agent ask a question. Guessing `schedule` for a human at a
    console would suppress a clarification they were waiting for."""
    assert run_context.from_payload({}).trigger == "chat"
    assert run_context.from_payload({"trigger": "  "}).trigger == "chat"


def test_a_run_id_is_generated_when_absent_so_every_run_is_correlatable():
    first = run_context.from_payload({})
    second = run_context.from_payload({})
    assert first.run_id != second.run_id
    assert len(first.run_id) == 36


def test_a_malformed_operating_date_is_refused_rather_than_silently_wrong():
    """The foundation's reporting endpoints are date-keyed. A date the agent
    cannot parse would query the wrong day, or no day, and look like no data."""
    with pytest.raises(ValueError, match="operatingDate must be YYYY-MM-DD"):
        run_context.from_payload({"operatingDate": "08/09/2026"})


def test_the_operating_date_defaults_to_today_in_utc():
    from datetime import datetime, timezone

    assert (
        run_context.from_payload({}).operating_date
        == datetime.now(timezone.utc).date().isoformat()
    )


def test_a_non_dict_payload_is_tolerated_rather_than_raising():
    """main.py validates the prompt separately; this must not be a second place a
    malformed payload can crash before the error is reportable."""
    assert run_context.from_payload(None).trigger == "chat"
    assert run_context.from_payload("nonsense").property_id is None


def test_non_string_caller_groups_are_dropped_not_stringified():
    ctx = run_context.from_payload({"callerGroups": ["Manager", 7, None]})
    assert ctx.caller_groups == ("Manager",)


# --------------------------------------------------------------------------- #
# Identity derived from the context
# --------------------------------------------------------------------------- #


def _service_pattern(field: str) -> str:
    """The pattern AgentCore Memory validates ``field`` against, from botocore's own
    model, so these tests track the service rather than a copy of its rules."""
    import botocore.session

    shape = botocore.session.get_session().get_service_model("bedrock-agentcore")
    return shape.operation_model("CreateEvent").input_shape.members[field].metadata["pattern"]


def test_memory_partitions_per_agent_per_property():
    """A durable fact about one hotel surfacing while reasoning about another would
    be a tenancy leak dressed up as a feature."""
    ctx = context(property_id="p-1")
    assert ctx.actor_id("arrivals") == "arrivals/p-1"
    assert ctx.actor_id("arrivals") != context(property_id="p-2").actor_id("arrivals")


def test_a_chain_wide_run_keeps_its_label_but_memory_gets_a_valid_one():
    ctx = context(property_id=None)
    assert ctx.scope_label == "_chain"  # headers and the decision log, unchanged
    assert ctx.actor_id("regional") == "regional/chain"


def test_no_two_runs_share_a_memory_session():
    """The security finding: one session per property per day let a front-desk user's
    chat become context for a Manager's run, or a scheduled one, the same day."""
    a = context(run_id="run-a")
    b = context(run_id="run-b")
    assert a.session_id != b.session_id
    # Same property, same day, different run: still not shared.
    assert a.memory_scope == b.memory_scope and a.operating_date == b.operating_date


@pytest.mark.parametrize("property_id", ["a1a1a1a1-0000-4000-8000-000000000001", None])
@pytest.mark.parametrize(
    "run_id", [str(uuid.uuid4()), "exec-Ab_-0123456789abcd", "x" * 120]
)
def test_memory_identifiers_are_ones_the_service_accepts(property_id, run_id):
    """They were not. The actor id used '#' and the session id ':', neither of which
    Memory's patterns allow, and the store held no events at all -- most likely
    because every write was rejected."""
    ctx = context(property_id=property_id, run_id=run_id)
    assert re.fullmatch(_service_pattern("sessionId"), ctx.session_id), ctx.session_id
    assert len(ctx.session_id) <= 100
    assert re.fullmatch(_service_pattern("actorId"), ctx.actor_id("night_audit"))


# --------------------------------------------------------------------------- #
# The preamble
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("audience", ["orchestrator", "specialist"])
def test_both_audiences_are_told_the_facts_of_the_run(audience):
    text = run_context.preamble("do the thing", context(), audience=audience)
    assert "runId: run-1" in text
    assert "propertyId: p-1" in text
    assert "operatingDate: 2026-09-08" in text
    assert "trigger: chat" in text
    # The request survives verbatim, last, so the model reads the scope first.
    assert text.rstrip().endswith("do the thing")


@pytest.mark.parametrize("audience", ["orchestrator", "specialist"])
def test_neither_audience_is_allowed_to_ask_questions_off_the_chat_trigger(audience):
    text = run_context.preamble("go", context(trigger="schedule"), audience=audience)
    assert "trigger: schedule" in text
    assert "do not" in text and "ask questions" in text


def test_the_orchestrator_is_told_the_request_may_not_name_the_property():
    """The regression. A scheduled prompt says "at this property" and nothing else;
    an orchestrator that does not know to look here asks a human instead."""
    text = run_context.preamble(
        "Pre-assign rooms for the unassigned arrivals at this property.",
        context(trigger="schedule"),
        audience="orchestrator",
    )
    assert "may not name the property" in text
    assert "Never ask which property or which date" in text
    # And it is told to pass the property on, since a specialist cannot guess it.
    assert "state propertyId explicitly in every delegation" in text


def test_the_specialist_is_told_to_pass_the_property_to_its_tools():
    text = run_context.preamble("go", context(), audience="specialist")
    assert "Use propertyId for every tool call that needs one" in text


def test_the_specialist_is_warned_not_to_collapse_a_forward_looking_window():
    """Also a regression: told to "use operatingDate for every tool call", A1
    re-queried list_arrivals with daysAhead=0, found nothing, and reported no
    arrivals at a property with 518 reservations."""
    text = run_context.preamble("go", context(), audience="specialist")
    assert "do not narrow a tool's own default window down to" in text


def test_only_the_specialist_is_told_to_refuse_a_conflicting_property():
    """A specialist must not act on a property the orchestrator names over the run
    scope. The orchestrator is the one doing the naming, so the same instruction
    would tell it to distrust itself."""
    specialist = run_context.preamble("go", context(), audience="specialist")
    orchestrator = run_context.preamble("go", context(), audience="orchestrator")
    assert "do not act on it" in specialist
    assert "do not act on it" not in orchestrator


def test_a_chain_wide_run_says_so_in_words_rather_than_showing_none():
    text = run_context.preamble("go", context(property_id=None), audience="specialist")
    assert "chain-wide" in text
    assert "propertyId: None" not in text


def test_caller_groups_appear_only_when_the_run_has_them():
    """The copilot path forwards the operator's real authority. A scheduled run has
    no operator, and an empty line would invite the model to invent one."""
    assert "callerGroups" not in run_context.preamble(
        "go", context(), audience="orchestrator"
    )
    text = run_context.preamble(
        "go", context(caller_groups=("Manager",)), audience="orchestrator"
    )
    assert "callerGroups: Manager" in text


def test_the_facts_are_a_user_turn_not_a_system_prompt_edit():
    """Bedrock caches the system prompt prefix. Splicing per-run values into it
    would invalidate that cache on every single invocation."""
    text = run_context.preamble("go", context(), audience="specialist")
    assert text.startswith("<run_context>")
    assert "</run_context>" in text
