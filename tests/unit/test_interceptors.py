# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two Gateway interceptors, offline.

These exist because of a bug that shipped and was caught by a live probe rather
than by a test: both files hard-coded a two-underscore tool-name delimiter while
the Gateway advertises three, so ``billing___post_charge`` matched neither the
approval gate's ``GATED_TOOLS`` nor the decision log's ``WRITE_TOOLS``. The gate
would have passed every Tier-2 write through, and the audit table would have
recorded every write as though nothing had been done.

A guardrail that fails open is worse than no guardrail, so the delimiter is now
parsed and the parsing is tested at both widths. Everything else here is the
payload contract: what the Gateway hands in, and what it must get back.

No AWS: both modules build their DynamoDB client lazily through a module-level
``_approvals`` / ``_decisions`` function, which is what these tests replace.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[2] / "infra" / "lambdas"


def _load(name: str, relative: str):
    """Import a Lambda handler by path.

    Both handlers are called ``index.py``, so they cannot be imported by module
    name -- the second would shadow the first.
    """
    spec = importlib.util.spec_from_file_location(name, LAMBDAS / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approval = _load("approval_interceptor_index", "approval_interceptor/index.py")
decision_log = _load("decision_log_index", "decision_log_interceptor/index.py")


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


class FakeTable:
    """Stands in for the DynamoDB client both handlers use."""

    def __init__(self, item=None, raises: Exception | None = None):
        self.item = item
        self.raises = raises
        self.puts: list[dict] = []
        self.gets: list[dict] = []

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        if self.raises:
            raise self.raises
        return {"Item": self.item} if self.item else {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        if self.raises:
            raise self.raises
        return {}


def approved(
    action: str,
    *,
    status: str = "APPROVED",
    expires_in: int = 600,
    **bindings,
) -> dict:
    """An approval record as the ops console writes one.

    ``bindings`` are the target and amount the approval is bound to -- ``folioId``,
    ``guestId``, ``amount``. Passed through as DynamoDB attributes so a test can
    say "approved for post_charge on folio f1 at 40" and mean exactly that. Numbers
    become ``N`` and everything else ``S``, which is what the console does.
    """
    item = {
        "approvalId": {"S": "tok-1"},
        "status": {"S": status},
        "action": {"S": action},
        approval.TTL_ATTRIBUTE: {"N": str(int(time.time()) + expires_in)},
    }
    for key, value in bindings.items():
        item[key] = (
            {"N": str(value)} if isinstance(value, (int, float)) else {"S": str(value)}
        )
    return item


def request_event(body, headers: dict | None = None) -> dict:
    """A request as the Gateway hands it to the request interceptor.

    The approval token no longer travels in a tool call's arguments: the Runtime
    sends it as ``X-Hotel-Ops-Approval-Token``, and the interceptor discards anything
    the model wrote into ``approval_token``. The binding tests below predate that and
    still spell the token as an argument, because that is where it ends up for the
    billing Lambda; this lifts it into the header the Runtime would have sent, so they
    keep testing the bindings they were written for. The tests that a model-written
    token is *ignored* build their events with :func:`raw_request_event` instead.
    """
    headers = dict(headers or {})
    for message in body if isinstance(body, list) else [body]:
        if not isinstance(message, dict):
            continue  # the malformed-body tests: nothing to lift
        arguments = (message.get("params") or {}).get("arguments") or {}
        if isinstance(arguments, dict) and "approval_token" in arguments:
            headers.setdefault("X-Hotel-Ops-Approval-Token", arguments["approval_token"])
    return raw_request_event(body, headers or None)


def raw_request_event(body, headers: dict | None = None) -> dict:
    """Exactly this body and these headers, nothing lifted or added."""
    event = {"mcp": {"gatewayRequest": {"body": body}}}
    if headers is not None:
        event["mcp"]["gatewayRequest"]["headers"] = headers
    return event


def tool_call(name: str, arguments: dict | None = None, call_id="c-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def refusal_of(response: dict) -> dict:
    """The parsed foundation envelope inside a short-circuit refusal."""
    body = response["mcp"]["transformedGatewayResponse"]["body"]
    message = body[0] if isinstance(body, list) else body
    return json.loads(message["result"]["content"][0]["text"])


def is_passthrough(response: dict) -> bool:
    return "transformedGatewayRequest" in response["mcp"]


@pytest.fixture
def table(monkeypatch):
    """Wire a FakeTable into the approval gate and return it for configuration."""
    fake = FakeTable()
    monkeypatch.setattr(approval, "_approvals", lambda: fake)
    monkeypatch.setattr(approval, "APPROVALS_TABLE", "hotel-ops-agent-approvals")
    return fake


# --------------------------------------------------------------------------- #
# Approval gate: the delimiter
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,expected",
    [
        ("billing___post_charge", ("billing", "post_charge")),
        ("billing__post_charge", ("billing", "post_charge")),
        ("housekeeping___assign_task", ("housekeeping", "assign_task")),
        # No delimiter: no target. Matches nothing gated, which is why the target
        # Lambda repeats the check.
        ("post_charge", ("", "post_charge")),
    ],
)
def test_tool_names_are_parsed_not_split_on_a_fixed_width(name, expected):
    assert approval._split_tool_name(name) == expected


@pytest.mark.parametrize("delimiter", ["__", "___", "____"])
def test_every_tier_2_tool_is_gated_at_any_delimiter_width(delimiter, table):
    """The regression. A gate that misses the live spelling is not a gate."""
    for action in ("post_charge", "void_folio", "adjust_loyalty"):
        response = approval.handler(
            request_event(tool_call(f"billing{delimiter}{action}")), None
        )
        assert not is_passthrough(response), f"{action} passed through ungated"
        assert refusal_of(response)["error"]["code"] == "APPROVAL_REQUIRED"


# --------------------------------------------------------------------------- #
# Approval gate: refusals
# --------------------------------------------------------------------------- #


def test_a_gated_call_with_no_token_is_refused_as_a_readable_tool_error():
    response = approval.handler(request_event(tool_call("billing___post_charge")), None)
    short_circuit = response["mcp"]["transformedGatewayResponse"]
    body = short_circuit["body"]

    # 200 with isError, not a JSON-RPC error: the model must be able to read this
    # and file a proposal instead of having its turn ended by a transport failure.
    assert short_circuit["statusCode"] == 200
    assert body["result"]["isError"] is True
    assert "error" not in body
    assert body["id"] == "c-1"

    envelope = refusal_of(response)
    assert envelope["success"] is False
    assert envelope["error"]["code"] == "APPROVAL_REQUIRED"
    assert envelope["error"]["details"] == {"tool": "billing___post_charge", "tier": 2}
    # The refusal names the action, so the proposal the model files is the right one.
    assert "post_charge" in envelope["error"]["message"]


def test_an_unconfigured_table_refuses_rather_than_rubber_stamps(monkeypatch):
    """Fail closed. This is the inversion that would make the gate decorative."""
    monkeypatch.setattr(approval, "APPROVALS_TABLE", "")
    response = approval.handler(
        request_event(tool_call("billing___post_charge", {"approval_token": "tok-1"})),
        None,
    )
    assert not is_passthrough(response)
    envelope = refusal_of(response)
    assert "cannot be verified" in envelope["error"]["message"]
    # Not APPROVAL_REQUIRED: no proposal a human files can fix a missing table,
    # and the decision log must not record broken infrastructure as a routine
    # request for approval.
    assert envelope["error"]["code"] == "APPROVAL_UNVERIFIABLE"


def test_a_lookup_failure_refuses_rather_than_assumes_approval(table):
    table.raises = RuntimeError("throttled")
    response = approval.handler(
        request_event(tool_call("billing___void_folio", {"approval_token": "tok-1"})),
        None,
    )
    assert not is_passthrough(response)
    envelope = refusal_of(response)
    assert "RuntimeError" in envelope["error"]["message"]
    assert envelope["error"]["code"] == "APPROVAL_UNVERIFIABLE"


@pytest.mark.parametrize(
    "item,code,fragment",
    [
        (None, "APPROVAL_INVALID", "does not exist"),
        (approved("post_charge", status="PENDING"), "APPROVAL_INVALID", "not APPROVED"),
        (approved("post_charge", status="REJECTED"), "APPROVAL_INVALID", "not APPROVED"),
        # An approval to void a folio must never authorize a charge.
        (approved("void_folio"), "APPROVAL_MISMATCH", "was granted for"),
        (approved("post_charge", expires_in=-1), "APPROVAL_INVALID", "expired"),
    ],
    ids=["missing", "pending", "rejected", "wrong-action", "expired"],
)
def test_an_invalid_approval_is_refused(table, item, code, fragment):
    table.item = item
    response = approval.handler(
        request_event(tool_call("billing___post_charge", {"approval_token": "tok-1"})),
        None,
    )
    assert not is_passthrough(response)
    envelope = refusal_of(response)
    assert fragment in envelope["error"]["message"]
    # The code, not just the prose: the ops console and the Phase-5 evaluators
    # read the code, and "invalid" and "mismatched" are different human stories.
    assert envelope["error"]["code"] == code


def test_a_valid_approval_passes_the_call_through(table):
    table.item = approved("post_charge", folioId="f1", amount=40, description="x")
    call = tool_call("billing___post_charge", {"folioId": "f1", "amount": 40, "description": "x", "approval_token": "tok-1"})
    response = approval.handler(request_event(call), None)

    assert is_passthrough(response)
    assert response["mcp"]["transformedGatewayRequest"]["body"] == call
    # Consistent read: a human approving in the console and the agent retrying
    # seconds later is the normal path.
    assert table.gets[0]["ConsistentRead"] is True


def test_the_approval_is_read_and_never_consumed(table):
    """The Gateway may retry an interceptor; burning a token would refuse a write
    the human did approve."""
    table.item = approved("post_charge", folioId="f1", amount=40, description="x")
    for _ in range(3):
        response = approval.handler(
            request_event(
                tool_call("billing___post_charge", {"folioId": "f1", "amount": 40, "description": "x", "approval_token": "tok-1"})
            ),
            None,
        )
        assert is_passthrough(response)
    assert table.puts == []


# --------------------------------------------------------------------------- #
# Approval gate: passthrough
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        tool_call("billing___list_folios", {"propertyId": "p1"}),
        tool_call("arrivals___assign_room", {"stayId": "s1", "roomId": "r1"}),
        tool_call("housekeeping___complete_task", {"taskId": "t1"}),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        # Shapes the gate cannot read are passed through: a body it cannot parse
        # is a body in which it cannot have found a gated tool.
        None,
        "not json at all",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 7}},
    ],
    ids=[
        "billing-read",
        "tier-1-assign-room",
        "tier-1-complete-task",
        "tools-list",
        "notification",
        "initialize",
        "null-body",
        "string-body",
        "no-params",
        "non-string-name",
    ],
)
def test_everything_that_is_not_tier_2_is_echoed_unchanged(body):
    response = approval.handler(request_event(body), None)
    assert is_passthrough(response)
    assert response["mcp"]["transformedGatewayRequest"]["body"] == body


def test_one_unapproved_call_refuses_the_whole_batch():
    """A batch cannot be partially short-circuited, so a gated call hidden among
    reads must take the whole request down with it."""
    body = [
        tool_call("billing___list_folios", {"propertyId": "p1"}, call_id="c-1"),
        tool_call("billing___post_charge", {"folioId": "f1"}, call_id="c-2"),
    ]
    response = approval.handler(request_event(body), None)

    assert not is_passthrough(response)
    returned = response["mcp"]["transformedGatewayResponse"]["body"]
    # A batched request gets a batched response, or the client cannot match it up.
    assert isinstance(returned, list)
    assert returned[0]["id"] == "c-2"
    assert refusal_of(response)["error"]["code"] == "APPROVAL_REQUIRED"


def test_the_interceptor_output_version_is_always_declared():
    for response in (
        approval.handler(request_event(tool_call("billing___list_folios")), None),
        approval.handler(request_event(tool_call("billing___post_charge")), None),
    ):
        assert response["interceptorOutputVersion"] == approval.OUTPUT_VERSION


# --------------------------------------------------------------------------- #
# Decision log: passthrough is sacred
# --------------------------------------------------------------------------- #


@pytest.fixture
def decisions(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(decision_log, "_decisions", lambda: fake)
    monkeypatch.setattr(decision_log, "DECISIONS_TABLE", "hotel-ops-agent-decisions")
    return fake


def response_event(request_body, response_body, headers=None, **response_extra) -> dict:
    """``headers`` are the *request's* correlation headers; anything in
    ``response_extra`` (``statusCode``, ``headers``) belongs to the response."""
    request: dict = {"body": request_body}
    if headers is not None:
        request["headers"] = headers
    return {
        "mcp": {
            "gatewayRequest": request,
            "gatewayResponse": {"body": response_body, **response_extra},
        }
    }


def result_for(call_id="c-1", *, text: str | None = None, is_error=False) -> dict:
    content = [{"type": "text", "text": text}] if text is not None else []
    result: dict = {"content": content}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": call_id, "result": result}


def logged(fake: FakeTable) -> dict:
    assert len(fake.puts) == 1, f"expected exactly one row, got {len(fake.puts)}"
    return fake.puts[0]["Item"]


def test_the_response_is_returned_byte_for_byte(decisions):
    body = result_for(text=json.dumps({"success": True, "data": {"roomId": "r1"}}))
    event = response_event(tool_call("arrivals___assign_room"), body, statusCode=200)
    event["mcp"]["gatewayResponse"]["headers"] = {"content-type": "application/json"}

    transformed = decision_log.handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert transformed == {
        "body": body,
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
    }


def test_absent_status_and_headers_are_not_invented(decisions):
    """On a later streaming chunk the gateway ignores these; fabricating a 200
    would be asserting something this invocation does not know."""
    body = result_for(text=json.dumps({"success": True}))
    response = decision_log.handler(
        response_event(tool_call("arrivals___assign_room"), body), None
    )
    assert response["mcp"]["transformedGatewayResponse"] == {"body": body}


def test_a_write_failure_never_fails_the_call(monkeypatch, decisions):
    """Audit matters; it is not more important than the room assignment."""
    decisions.raises = RuntimeError("table gone")
    body = result_for(text=json.dumps({"success": True}))
    response = decision_log.handler(
        response_event(tool_call("arrivals___assign_room"), body), None
    )
    assert response["mcp"]["transformedGatewayResponse"]["body"] == body


# --------------------------------------------------------------------------- #
# Decision log: what gets recorded
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("delimiter", ["__", "___"])
def test_a_successful_write_records_action_taken_at_any_delimiter(delimiter, decisions):
    """The other half of the regression: the audit table must not record a real
    write as though nothing happened."""
    decision_log.handler(
        response_event(
            tool_call(f"arrivals{delimiter}assign_room", {"stayId": "s1"}),
            result_for(text=json.dumps({"success": True, "data": {"roomId": "r1"}})),
        ),
        None,
    )
    item = logged(decisions)
    assert item["action_taken"] == {"S": "assign_room"}
    assert item["outcome"] == {"S": "ok"}


def test_a_read_is_logged_but_is_not_an_action(decisions):
    decision_log.handler(
        response_event(
            tool_call("arrivals___list_arrivals", {"propertyId": "p1"}),
            result_for(text=json.dumps({"success": True, "data": []})),
        ),
        None,
    )
    item = logged(decisions)
    assert "action_taken" not in item
    assert item["tool"] == {"S": "arrivals___list_arrivals"}


def test_a_refused_write_is_not_recorded_as_money_moved(decisions):
    """The row for a blocked Tier-2 charge must not read as a completed charge."""
    decision_log.handler(
        response_event(
            tool_call("billing___post_charge", {"folioId": "f1"}),
            result_for(
                text=json.dumps(
                    {
                        "success": False,
                        "error": {"code": "APPROVAL_REQUIRED", "message": "no"},
                    }
                ),
                is_error=True,
            ),
        ),
        None,
    )
    item = logged(decisions)
    assert "action_taken" not in item
    assert item["outcome"] == {"S": "tool_error"}
    assert item["error_code"] == {"S": "APPROVAL_REQUIRED"}


def test_a_200_carrying_a_failed_envelope_is_not_recorded_as_ok(decisions):
    """The tool layer returns the foundation's envelope verbatim, so an MCP
    success can still be a business failure."""
    decision_log.handler(
        response_event(
            tool_call("arrivals___check_in", {"stayId": "s1"}),
            result_for(
                text=json.dumps(
                    {"success": False, "error": {"code": "InvalidStateError"}}
                )
            ),
        ),
        None,
    )
    item = logged(decisions)
    assert item["outcome"] == {"S": "failed"}
    assert item["error_code"] == {"S": "InvalidStateError"}
    assert "action_taken" not in item


def test_correlation_headers_become_the_row_identity(decisions):
    decision_log.handler(
        response_event(
            tool_call("housekeeping___assign_task", {"taskId": "t1"}),
            result_for(text=json.dumps({"success": True})),
            headers={
                # Upper-cased on purpose: header case is not guaranteed to survive.
                "X-Hotel-Ops-Run-Id": "run-9",
                "X-Hotel-Ops-Property-Id": "prop-3",
                "X-Hotel-Ops-Agent": "housekeeping",
                "X-Hotel-Ops-Operating-Date": "2026-09-08",
                "X-Hotel-Ops-Trigger": "schedule",
            },
        ),
        None,
    )
    item = logged(decisions)
    assert item["run_id"] == {"S": "run-9"}
    assert item["property_id"] == {"S": "prop-3"}
    assert item["agent"] == {"S": "housekeeping"}
    assert item["operating_date"] == {"S": "2026-09-08"}
    assert item["trigger"] == {"S": "schedule"}


def test_a_call_from_outside_the_agent_graph_is_still_recorded(decisions):
    """Exactly the thing an audit table should show rather than discard."""
    decision_log.handler(
        response_event(
            tool_call("billing___list_folios", {"propertyId": "p1"}),
            result_for(text=json.dumps({"success": True})),
        ),
        None,
    )
    item = logged(decisions)
    assert item["run_id"] == {"S": "unattributed"}
    assert item["property_id"] == {"S": "_unknown"}
    # No agent header, so the target name is the honest fallback.
    assert item["agent"] == {"S": "billing"}


def test_arguments_are_hashed_never_stored(decisions):
    """They carry guest names, folio detail, and the approval token itself."""
    secret = {"folioId": "f1", "approval_token": "tok-secret", "guest": "A. Person"}
    decision_log.handler(
        response_event(
            tool_call("billing___post_charge", secret),
            result_for(text=json.dumps({"success": True})),
        ),
        None,
    )
    item = logged(decisions)
    serialized = json.dumps(item)
    assert "tok-secret" not in serialized
    assert "A. Person" not in serialized
    assert len(item["inputs_hash"]["S"]) == 64


def test_the_hash_is_stable_across_key_order(decisions):
    for arguments in ({"a": 1, "b": 2}, {"b": 2, "a": 1}):
        decision_log.handler(
            response_event(
                tool_call("billing___post_charge", arguments),
                result_for(text=json.dumps({"success": True})),
            ),
            None,
        )
    first, second = (put["Item"]["inputs_hash"]["S"] for put in decisions.puts)
    assert first == second


# --------------------------------------------------------------------------- #
# Decision log: what is deliberately not recorded
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "request_body,response_body",
    [
        # Protocol chatter is not a decision.
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"result": {"tools": []}}),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"result": {}}),
        # A server-initiated request arriving mid-stream carries `method`, not the
        # tool's result. With streaming on, this interceptor fires for it too.
        (
            tool_call("arrivals___assign_room"),
            {"jsonrpc": "2.0", "id": "x", "method": "elicitation/create"},
        ),
        # An id that does not echo the request is not this call's answer.
        (tool_call("arrivals___assign_room", call_id="c-1"), result_for("other")),
    ],
    ids=["tools-list", "notification", "server-request", "mismatched-id"],
)
def test_non_decisions_are_not_logged(decisions, request_body, response_body):
    decision_log.handler(response_event(request_body, response_body), None)
    assert decisions.puts == []


def test_nothing_is_written_before_the_table_exists(monkeypatch):
    """Phase 2 ships ahead of orchestration_stack, so this is the normal state
    until it lands -- and it must not break a tool call."""
    monkeypatch.setattr(decision_log, "DECISIONS_TABLE", "")
    called = False

    def _fail():
        nonlocal called
        called = True
        raise AssertionError("built a DynamoDB client with no table configured")

    monkeypatch.setattr(decision_log, "_decisions", _fail)
    body = result_for(text=json.dumps({"success": True}))
    response = decision_log.handler(
        response_event(tool_call("arrivals___assign_room"), body), None
    )
    assert response["mcp"]["transformedGatewayResponse"]["body"] == body
    assert not called


# --------------------------------------------------------------------------- #
# The approval is bound to a target and an amount, not just to an action
# --------------------------------------------------------------------------- #
#
# Matching on the action alone is not enough, and the gap is not subtle: an approval
# to post a $40 late-checkout charge would authorize a $40 charge on any folio in the
# chain, because post_charge == post_charge. One over-broad approval would become a
# general licence to bill. These pin the bindings the ops console records.


def test_an_approval_bound_to_one_folio_does_not_release_another(table):
    """The regression this binding exists for."""
    table.item = approved("post_charge", folioId="folio-approved", amount=40)
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {
                    "folioId": "folio-SOMEONE-ELSE",
                    "amount": 40,
                    "description": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert not is_passthrough(response)
    error = refusal_of(response)["error"]
    assert error["code"] == "APPROVAL_MISMATCH"
    assert "folio-approved" in error["message"]


def test_the_approved_amount_is_the_amount_that_may_move(table):
    table.item = approved("post_charge", folioId="f1", amount=40)
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {
                    "folioId": "f1",
                    "amount": 4000,
                    "description": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_MISMATCH"


def test_omitting_the_amount_entirely_does_not_evade_the_binding(table):
    """A missing field must not read as a match. The tool would default it."""
    table.item = approved("post_charge", folioId="f1", amount=40)
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {"folioId": "f1", "description": "x", "approval_token": "tok-1"},
            )
        ),
        None,
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_MISMATCH"


def test_a_non_numeric_amount_is_refused_rather_than_compared_as_text(table):
    table.item = approved("post_charge", folioId="f1", amount=40)
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {
                    "folioId": "f1",
                    "amount": "forty",
                    "description": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_MISMATCH"


def test_an_amount_that_matches_numerically_passes_despite_a_type_difference(table):
    """40 and "40.00" are the same money. The console writes a Decimal and the model
    writes a JSON number, so a string comparison here would refuse valid approvals."""
    table.item = approved("post_charge", folioId="f1", amount="40.00", description="x")
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {
                    "folioId": "f1",
                    "amount": 40,
                    "description": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert is_passthrough(response)


def test_a_loyalty_approval_is_bound_to_the_guest(table):
    table.item = approved(
        "adjust_loyalty", guestId="guest-1", points=500, adjustReason="x"
    )
    refused = approval.handler(
        request_event(
            tool_call(
                "billing___adjust_loyalty",
                {
                    "guestId": "guest-2",
                    "points": 500,
                    "reason": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert refusal_of(refused)["error"]["code"] == "APPROVAL_MISMATCH"

    allowed = approval.handler(
        request_event(
            tool_call(
                "billing___adjust_loyalty",
                {
                    "guestId": "guest-1",
                    "points": 500,
                    "reason": "x",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert is_passthrough(allowed)


def test_a_fully_matching_approval_opens_the_gate(table):
    table.item = approved(
        "post_charge", folioId="f1", amount=40, description="Late checkout"
    )
    response = approval.handler(
        request_event(
            tool_call(
                "billing___post_charge",
                {
                    "folioId": "f1",
                    "amount": 40,
                    "description": "Late checkout",
                    "approval_token": "tok-1",
                },
            )
        ),
        None,
    )
    assert is_passthrough(response), "a correct approval must actually work"


def test_an_approval_that_records_no_bindings_releases_nothing(table):
    """This test once asserted the opposite: that an approval recording no target
    still worked for its action, so it would void *any* folio. A security review
    rejected that -- absent is not unbound -- and the gate now refuses it."""
    table.item = approved("void_folio")
    response = approval.handler(
        request_event(
            tool_call(
                "billing___void_folio",
                {"folioId": "anything", "reason": "x", "approval_token": "tok-1"},
            )
        ),
        None,
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_INCOMPLETE"


def test_a_loyalty_approval_is_bound_to_its_points(table):
    """The finding: an approval for 100 points released 100,000."""
    table.item = approved("adjust_loyalty", guestId="g1", points=100, adjustReason="x")
    response = approval.handler(
        request_event(
            tool_call(
                "billing___adjust_loyalty",
                {"guestId": "g1", "points": 100000, "reason": "x", "approval_token": "tok-1"},
            )
        ),
        None,
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_MISMATCH"


@pytest.mark.parametrize(
    "extra",
    [{"chargeDate": "2020-01-01"}, {"chargeType": "ADJUSTMENT"}, {"description": "else"}],
    ids=["back-dated", "different-charge-type", "different-description"],
)
def test_nothing_the_approval_did_not_name_can_be_changed_or_added(table, extra):
    table.item = approved("post_charge", folioId="f1", amount=40, description="x")
    args = {"folioId": "f1", "amount": 40, "description": "x", "approval_token": "tok-1"}
    response = approval.handler(
        request_event(tool_call("billing___post_charge", {**args, **extra})), None
    )
    assert refusal_of(response)["error"]["code"] == "APPROVAL_MISMATCH"


def test_the_default_charge_type_is_accepted_when_spelled_out(table):
    """A model that helpfully writes chargeType=SERVICE is not refused for it."""
    table.item = approved("post_charge", folioId="f1", amount=40, description="x")
    args = {"folioId": "f1", "amount": 40, "description": "x", "chargeType": "SERVICE",
            "approval_token": "tok-1"}
    assert is_passthrough(
        approval.handler(request_event(tool_call("billing___post_charge", args)), None)
    )
