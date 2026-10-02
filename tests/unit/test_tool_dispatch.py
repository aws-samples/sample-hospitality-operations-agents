# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Gateway dispatch contract.

The prefix stripper gets its own tests because it is the one piece of glue that
sits between a name the Gateway chooses and a name the handler registered. If it
is wrong, every tool on every target fails identically and the model is given no
usable signal about why.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hotel_ops.tool_dispatch import (
    ToolRouter,
    strip_target_prefix,
    tool_error,
    tool_name_from,
)


def gateway_context(tool_name: str | None) -> SimpleNamespace:
    """A Lambda context shaped the way AgentCore Gateway sends one."""
    custom = {} if tool_name is None else {"bedrockAgentCoreToolName": tool_name}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


# --------------------------------------------------------------------------- #
# Prefix stripping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "qualified,target,expected",
    [
        # What the deployed Gateway actually sends: THREE underscores. The whole
        # run is consumed, so no leading underscore survives into the lookup key.
        ("housekeeping___assign_task", "housekeeping", "assign_task"),
        ("billing___post_charge", "billing", "post_charge"),
        # Two underscores must work identically. The delimiter width is the
        # Gateway's business, not ours.
        ("billing__post_charge", "billing", "post_charge"),
        # No prefix at all: pass through rather than mangle. A local invocation
        # or a future Gateway change that stops prefixing must still dispatch.
        ("assign_task", "billing", "assign_task"),
        # A tool whose own name contains "__" survives, because the prefix is
        # removed by the length of the known target rather than by searching.
        ("target___weird__tool", "target", "weird__tool"),
        # The target name must be followed by a delimiter to count. "billingadmin"
        # is a different target, and eating "billing" off it would route another
        # target's tool into this one.
        ("billingadmin___post_charge", "billing", "post_charge"),
        # Same call with no target supplied: the generic fallback still strips a
        # leading identifier plus its underscore run.
        ("housekeeping___assign_task", None, "assign_task"),
        ("assign_task", None, "assign_task"),
    ],
)
def test_strip_target_prefix(qualified, target, expected):
    assert strip_target_prefix(qualified, target) == expected


def test_tool_name_from_context():
    assert (
        tool_name_from(gateway_context("arrivals___list_rooms"), "arrivals")
        == "list_rooms"
    )


@pytest.mark.parametrize(
    "context",
    [
        gateway_context(None),
        gateway_context(""),
        SimpleNamespace(client_context=None),
        SimpleNamespace(),
    ],
    ids=["no-key", "empty", "no-client-context", "bare-context"],
)
def test_tool_name_from_rejects_a_non_gateway_invocation(context):
    """A direct console invoke must fail loudly, not guess a tool."""
    with pytest.raises(KeyError):
        tool_name_from(context)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


@pytest.fixture
def router() -> ToolRouter:
    r = ToolRouter("demo")

    @r.tool("ok")
    def _ok(args):
        return {"success": True, "data": args}

    @r.tool("needs_arg")
    def _needs_arg(args):
        return {"success": True, "data": args["required_key"]}

    @r.tool("bad_value")
    def _bad_value(_args):
        raise ValueError("that date is not YYYY-MM-DD")

    @r.tool("explodes")
    def _explodes(_args):
        raise RuntimeError("connection reset")

    return r


def test_tool_names_are_sorted(router):
    assert router.tool_names == ["bad_value", "explodes", "needs_arg", "ok"]


def test_duplicate_registration_is_rejected_at_import_time(router):
    """Two tools sharing a name would make dispatch silently ambiguous."""
    with pytest.raises(ValueError, match="already registered"):
        router.tool("ok")(lambda args: args)


def test_dispatch_strips_the_prefix_and_passes_the_event_as_arguments(router):
    result = router.dispatch({"a": 1}, gateway_context("demo__ok"))
    assert result == {"success": True, "data": {"a": 1}}


def test_dispatch_returns_the_handler_result_verbatim():
    """§4.3: the foundation's envelope reaches the model unmodified."""
    router = ToolRouter("demo")
    envelope = {
        "success": False,
        "error": {"code": "INVALID_STATE", "message": "task is COMPLETED"},
    }

    @router.tool("passthrough")
    def _passthrough(_args):
        return envelope

    assert router.dispatch({}, gateway_context("demo__passthrough")) is envelope


def test_unknown_tool_names_what_is_available(router):
    result = router.dispatch({}, gateway_context("demo___nope"))
    assert result["success"] is False
    assert result["error"]["code"] == "UNKNOWN_TOOL"
    # The model can recover from this; a bare failure would leave it guessing.
    assert result["error"]["details"]["available"] == router.tool_names


def test_a_tool_routed_to_the_wrong_target_is_refused(router):
    """The cross-agent isolation case: borrowing another target's tool name."""
    result = router.dispatch({}, gateway_context("billing___post_charge"))
    assert result["error"]["code"] == "UNKNOWN_TOOL"


@pytest.mark.parametrize(
    "tool,code,fragment",
    [
        ("needs_arg", "MISSING_ARGUMENT", "required_key"),
        ("bad_value", "INVALID_ARGUMENT", "YYYY-MM-DD"),
        ("explodes", "TOOL_ERROR", "RuntimeError"),
    ],
)
def test_exceptions_become_actionable_error_envelopes(router, tool, code, fragment):
    result = router.dispatch({}, gateway_context(f"demo__{tool}"))
    assert result["success"] is False
    assert result["error"]["code"] == code
    assert fragment in result["error"]["message"]


def test_dispatch_never_raises(router):
    """A raised exception would surface to the model as an opaque Lambda error."""
    for context in (gateway_context(None), gateway_context("demo__explodes")):
        assert router.dispatch({}, context)["success"] is False


def test_a_non_dict_event_is_treated_as_no_arguments(router):
    assert router.dispatch(None, gateway_context("demo__ok"))["data"] == {}
    assert router.dispatch("junk", gateway_context("demo__ok"))["data"] == {}


def test_tool_error_omits_the_details_key_when_there_is_nothing_to_say():
    assert tool_error("X", "y") == {"success": False, "error": {"code": "X", "message": "y"}}
    assert tool_error("X", "y", n=1)["error"]["details"] == {"n": 1}
