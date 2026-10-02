# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A run is about one hotel, on one person's authority, and the model cannot widen it.

These exist because of a threat-model finding that turned out to be real. The run's
property reached the tools only as prompt text, and every tool identity except
housekeeping is chain-level -- so nothing *mechanical* stopped a front-desk user at
one hotel from having the copilot read another hotel's folios by naming one, and a
released approval could have moved money on a folio at a hotel the approver was never
scoped to. ``agents/run_context.py`` even claimed the guarantee in its docstring.

Now the Runtime sends the run's facts as Gateway headers the model has no channel to,
the request interceptor enforces them, and the tool Lambdas prove that any record
named only by id is at the run's property before they touch it. The approval token
travels the same way, so the model never sees it -- it used to be in the prompt, which
AgentCore Memory stored and traces recorded.

Fully offline.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAMBDAS = REPO / "infra" / "lambdas"
AGENTS = REPO / "agents"

HOTEL_A = "a1a1a1a1-0000-4000-8000-000000000001"
HOTEL_B = "b2b2b2b2-0000-4000-8000-000000000002"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


interceptor = _load("scope_interceptor", LAMBDAS / "approval_interceptor" / "index.py")


# --------------------------------------------------------------------------- #
# The Gateway request interceptor
# --------------------------------------------------------------------------- #


def call(name: str, arguments: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "c-1",
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments or {})},
    }


def run(body, *, property_id=HOTEL_A, groups=None, token=None) -> dict:
    """Invoke the interceptor as the Gateway would, for one run's headers."""
    headers = {}
    if property_id is not None:
        headers["X-Hotel-Ops-Property-Id"] = property_id
    if groups is not None:
        headers["X-Hotel-Ops-Caller-Groups"] = ",".join(groups)
    if token is not None:
        headers["X-Hotel-Ops-Approval-Token"] = token
    event = {"mcp": {"gatewayRequest": {"body": body, "headers": headers}}}
    return interceptor.handler(event, None)


def refused(response: dict) -> dict | None:
    """The error inside a refusal, or ``None`` if the call was passed through."""
    mcp = response["mcp"]
    if "transformedGatewayRequest" in mcp:
        return None
    body = mcp["transformedGatewayResponse"]["body"]
    return json.loads(body["result"]["content"][0]["text"])["error"]


def forwarded(response: dict) -> dict:
    """The arguments the target Lambda will actually receive."""
    return response["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]


class FakeApprovals:
    def __init__(self, item: dict | None):
        self.item = item

    def get_item(self, **_):
        return {"Item": self.item} if self.item else {}


def approval_for(action: str, *, property_id=HOTEL_A, **bindings) -> dict:
    item = {
        "approvalId": {"S": "apv-released"},
        "status": {"S": "APPROVED"},
        "action": {"S": action},
        "propertyId": {"S": property_id},
        "expiresAt": {"N": str(int(time.time()) + 600)},
    }
    for key, value in bindings.items():
        item[key] = {"N": str(value)} if isinstance(value, (int, float)) else {"S": value}
    return item


@pytest.fixture
def approvals(monkeypatch):
    def install(item):
        monkeypatch.setattr(interceptor, "APPROVALS_TABLE", "approvals")
        monkeypatch.setattr(interceptor, "_approvals", lambda: FakeApprovals(item))

    return install


# --- property scope -------------------------------------------------------- #


def test_a_call_naming_another_hotel_is_refused_before_the_tool_runs():
    """The finding itself: a folio id or property from a chat must not widen scope."""
    error = refused(run(call("billing___list_folios", {"propertyId": HOTEL_B})))
    assert error["code"] == "OUT_OF_SCOPE"
    assert HOTEL_A in error["message"]


def test_a_call_naming_this_runs_hotel_passes_through():
    assert refused(run(call("billing___list_folios", {"propertyId": HOTEL_A}))) is None


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("regional___occupancy", {}),
        ("regional___list_properties", {}),
        ("regional___range_metrics", {}),
        ("regional___range_metrics", {"propertyId": "_all"}),
    ],
    ids=["occupancy", "list_properties", "range-no-property", "range-all"],
)
def test_chain_wide_tools_are_out_of_reach_of_a_property_scoped_run(tool, arguments):
    """Portfolio figures are not something a single-hotel run may see."""
    assert refused(run(call(tool, arguments)))["code"] == "OUT_OF_SCOPE"


@pytest.mark.parametrize(
    "tool",
    ["arrivals___get_loyalty_profile", "billing___get_loyalty_profile",
     "billing___get_loyalty_transactions"],
)
def test_guest_loyalty_reads_are_property_neutral(tool):
    """Guests are chain-level records; the platform scopes loyalty by nothing else."""
    assert refused(run(call(tool, {"guestId": "g1"}))) is None


def test_a_chain_wide_run_may_name_any_property():
    response = run(call("billing___list_folios", {"propertyId": HOTEL_B}), property_id="_chain")
    assert refused(response) is None


def test_a_call_from_outside_the_agent_graph_is_not_scoped():
    """No header means an IAM-authorized operator, not the model; logged, not refused."""
    response = run(call("billing___list_folios", {"propertyId": HOTEL_B}), property_id=None)
    assert refused(response) is None


# --- caller authority ------------------------------------------------------ #


@pytest.mark.parametrize(
    "groups,tool,arguments,allowed",
    [
        # The platform reserves pre-assignment for Managers.
        (["FrontDesk"], "arrivals___assign_room", {"propertyId": HOTEL_A}, False),
        (["Manager"], "arrivals___assign_room", {"propertyId": HOTEL_A}, True),
        # Housekeeping cannot read folios on the platform, so not through the copilot.
        (["Housekeeping"], "billing___get_folio", {"propertyId": HOTEL_A}, False),
        (["FrontDesk"], "billing___get_folio", {"propertyId": HOTEL_A}, True),
        # And a front-desk agent cannot run housekeeping.
        (["FrontDesk"], "housekeeping___assign_task", {"propertyId": HOTEL_A}, False),
        (["Housekeeping"], "housekeeping___assign_task", {"propertyId": HOTEL_A}, True),
        # Reports belong to the reporting groups.
        (["FrontDesk"], "nightaudit___audit_report", {"propertyId": HOTEL_A}, False),
    ],
)
def test_the_copilot_acts_with_the_askers_authority_not_the_agents(
    groups, tool, arguments, allowed
):
    error = refused(run(call(tool, arguments), groups=groups))
    assert (error is None) is allowed
    if not allowed:
        assert error["code"] == "NOT_PERMITTED_FOR_CALLER"


def test_a_scheduled_run_has_no_asker_and_uses_the_agents_own_identity():
    """No groups header: a schedule or an event, where the tool identity is the authority."""
    response = run(call("arrivals___assign_room", {"propertyId": HOTEL_A}), groups=None)
    assert refused(response) is None


def test_a_tool_missing_from_the_authority_map_is_refused_in_a_human_run():
    """Fail closed: a tool added later must not be silently open to everyone."""
    response = run(call("billing___brand_new_tool", {"propertyId": HOTEL_A}), groups=["Admin"])
    assert refused(response)["code"] == "NOT_PERMITTED_FOR_CALLER"


def test_every_advertised_tool_has_an_authority_rule():
    """The map is transcribed from the platform; this keeps it complete."""
    advertised = {
        (path.stem, tool["name"])
        for path in (REPO / "schemas").glob("*.json")
        for tool in json.loads(path.read_text())
    }
    assert advertised == set(interceptor.TOOL_GROUPS)


def test_every_tool_without_a_property_is_a_deliberate_decision():
    """A new tool with no propertyId must be classified, not inherit a default."""
    no_property = {
        (path.stem, tool["name"])
        for path in (REPO / "schemas").glob("*.json")
        for tool in json.loads(path.read_text())
        if "propertyId" not in tool["inputSchema"].get("required", [])
    }
    chain_wide = {
        ("regional", "list_properties"),
        ("regional", "occupancy"),
        ("regional", "range_metrics"),
    }
    assert no_property == set(interceptor.PROPERTY_NEUTRAL) | chain_wide


# --- the approval travels out of band --------------------------------------- #

CHARGE = {"propertyId": HOTEL_A, "folioId": "f1", "amount": 40, "description": "x"}


def test_a_token_written_by_the_model_is_not_an_approval(approvals):
    """Even a *real* released token, if it arrives as an argument, opens nothing."""
    approvals(approval_for("post_charge", folioId="f1", amount=40, description="x"))
    response = run(call("billing___post_charge", {**CHARGE, "approval_token": "apv-released"}))
    assert refused(response)["code"] == "APPROVAL_REQUIRED"


def test_the_released_approval_arrives_as_a_header_and_is_attached_for_the_lambda(approvals):
    approvals(approval_for("post_charge", folioId="f1", amount=40, description="x"))
    response = run(call("billing___post_charge", CHARGE), token="apv-released")  # nosec B106 - test fake
    assert refused(response) is None
    assert forwarded(response)["approval_token"] == "apv-released"


def test_a_model_written_token_is_replaced_not_forwarded(approvals):
    approvals(approval_for("post_charge", folioId="f1", amount=40, description="x"))
    response = run(
        call("billing___post_charge", {**CHARGE, "approval_token": "apv-model-invented"}),
        token="apv-released",  # nosec B106 - test fake
    )
    assert forwarded(response)["approval_token"] == "apv-released"


def test_an_approval_filed_at_one_hotel_cannot_move_money_at_another(approvals):
    """T005: the approver's property is the approval's property, and the run's."""
    approvals(approval_for("post_charge", property_id=HOTEL_B, folioId="f1", amount=40, description="x"))
    response = run(call("billing___post_charge", CHARGE), token="apv-released")  # nosec B106 - test fake
    assert refused(response)["code"] == "APPROVAL_MISMATCH"


def test_scope_is_checked_before_the_approval(approvals):
    """A run pinned to hotel A cannot spend even a valid approval on hotel B."""
    approvals(approval_for("post_charge", property_id=HOTEL_B, folioId="f1", amount=40, description="x"))
    response = run(
        call("billing___post_charge", {**CHARGE, "propertyId": HOTEL_B}), token="apv-released"  # nosec B106 - test fake
    )
    assert refused(response)["code"] == "OUT_OF_SCOPE"


# --------------------------------------------------------------------------- #
# The tool Lambdas: a record named only by id must be proven to be here
# --------------------------------------------------------------------------- #

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_handlers import FakeDynamo, ROOM_SUMMARY, approval, ok, wire, wire_billing  # noqa: E402


def folio_at(property_id: str) -> dict:
    return {"GET /billing/folios/f1": (200, ok({"folioId": "f1", "propertyId": property_id}))}


def test_a_folio_at_another_hotel_is_refused_and_nothing_of_it_is_returned(monkeypatch):
    module, _ = wire_billing(monkeypatch, routes=folio_at(HOTEL_B))
    result = module.get_folio({"propertyId": HOTEL_A, "folioId": "f1"})
    assert result["error"]["code"] == "OUT_OF_SCOPE"
    assert "data" not in result, "no line of another hotel's folio may reach the model"


def test_no_charge_is_posted_to_a_folio_at_another_hotel(monkeypatch):
    routes = {**folio_at(HOTEL_B), "POST /billing/folios/f1/charges": (200, ok({}))}
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(approval(action="post_charge", folioId="f1", amount=40, description="x")),
        routes=routes,
    )
    result = module.post_charge({**CHARGE, "approval_token": "t"})
    assert result["error"]["code"] == "OUT_OF_SCOPE"
    assert not [c for c in fake.calls if c["method"] == "POST"]


def test_the_billing_lambda_checks_the_approvals_property_independently(monkeypatch):
    """Defence in depth: the interceptor checks this too, and either alone must hold."""
    module, fake = wire_billing(
        monkeypatch,
        dynamo=FakeDynamo(
            approval(action="post_charge", folioId="f1", amount=40, propertyId=HOTEL_B)
        ),
        routes={**folio_at(HOTEL_A), "POST /billing/folios/f1/charges": (200, ok({}))},
    )
    result = module.post_charge({**CHARGE, "approval_token": "t"})
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert fake.calls == []


def test_a_room_at_another_hotel_is_never_assigned(monkeypatch):
    module, fake = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "PUT /stays/res1/assign-room": (200, ok({})),
        },
    )
    result = module.assign_room(
        {"propertyId": HOTEL_A, "reservationId": "res1", "roomId": "room-elsewhere", "reason": "x"}
    )
    assert result["error"]["code"] == "OUT_OF_SCOPE"
    assert not [c for c in fake.calls if c["method"] == "PUT"]


def test_a_reservation_that_is_not_arriving_here_is_never_checked_in(monkeypatch):
    stays = ok({"stays": [{"reservationId": "someone-else", "checkInDate": "2000-01-01"}],
                "pagination": {"totalPages": 1}})
    module, fake = wire(
        monkeypatch,
        "arrivals",
        {"GET /stays": (200, stays), "POST /stays/res1/checkin": (200, ok({}))},
    )
    result = module.check_in({"propertyId": HOTEL_A, "reservationId": "res1"})
    assert result["error"]["code"] == "OUT_OF_SCOPE"
    assert not [c for c in fake.calls if c["method"] == "POST"]


def test_check_in_refuses_a_room_the_platform_would_not_have_checked(monkeypatch):
    """The platform's check-in accepts any roomId without a property check."""
    stays = ok({"stays": [{"reservationId": "res1", "checkInDate": "2000-01-01"}],
                "pagination": {"totalPages": 1}})
    module, fake = wire(
        monkeypatch,
        "arrivals",
        {
            "GET /stays": (200, stays),
            "GET /housekeeping/rooms/summary": (200, ROOM_SUMMARY),
            "POST /stays/res1/checkin": (200, ok({})),
        },
    )
    result = module.check_in(
        {"propertyId": HOTEL_A, "reservationId": "res1", "roomId": "room-elsewhere"}
    )
    assert result["error"]["code"] == "OUT_OF_SCOPE"
    assert not [c for c in fake.calls if c["method"] == "POST"]


# --------------------------------------------------------------------------- #
# The Runtime: what goes in headers, and what never goes in text
# --------------------------------------------------------------------------- #


def _load_gateway():
    """``agents/gateway.py`` with its third-party imports stubbed.

    The offline suite does not install Strands. Only ``correlation_headers`` is under
    test, and it touches none of them.
    """
    stubs = {
        "strands": types.ModuleType("strands"),
        "strands.tools": types.ModuleType("strands.tools"),
        "strands.tools.mcp": types.ModuleType("strands.tools.mcp"),
        "strands.tools.mcp.mcp_client": types.ModuleType("strands.tools.mcp.mcp_client"),
        "strands.types": types.ModuleType("strands.types"),
        "strands.types.tools": types.ModuleType("strands.types.tools"),
        "sigv4": types.ModuleType("sigv4"),
    }
    stubs["strands.tools.mcp"].MCPClient = object
    stubs["strands.tools.mcp.mcp_client"].ToolFilters = dict
    stubs["strands.types.tools"].AgentTool = object
    stubs["sigv4"].SigV4Signer = object
    for name, module in stubs.items():
        sys.modules.setdefault(name, module)
    sys.path.insert(0, str(AGENTS))
    import run_context  # the same module instance gateway.py imports

    return _load("scope_gateway", AGENTS / "gateway.py"), run_context


gateway, run_context = _load_gateway()


def test_a_human_run_sends_the_askers_groups_and_a_released_approval_as_headers():
    ctx = run_context.from_payload(
        {"propertyId": HOTEL_A, "callerGroups": ["Manager"], "approvalToken": "apv-x",
         "operatingDate": "2026-01-01"}
    )
    run_context.set_current(ctx)
    headers = gateway.correlation_headers("billing")
    assert headers["X-Hotel-Ops-Property-Id"] == HOTEL_A
    assert headers["X-Hotel-Ops-Caller-Groups"] == "Manager"
    assert headers["X-Hotel-Ops-Approval-Token"] == "apv-x"


def test_a_scheduled_run_sends_neither():
    run_context.set_current(
        run_context.from_payload(
            {"propertyId": HOTEL_A, "trigger": "schedule", "operatingDate": "2026-01-01"}
        )
    )
    headers = gateway.correlation_headers("arrivals")
    assert "X-Hotel-Ops-Caller-Groups" not in headers
    assert "X-Hotel-Ops-Approval-Token" not in headers


def test_the_approval_token_never_reaches_the_models_text():
    """Neither the preamble every agent reads nor a logged context carries it."""
    ctx = run_context.from_payload(
        {"propertyId": HOTEL_A, "approvalToken": "apv-secret", "operatingDate": "2026-01-01"}
    )
    assert "apv-secret" not in run_context.preamble("post the charge", ctx, audience="billing")
    assert "apv-secret" not in repr(ctx)


# --------------------------------------------------------------------------- #
# Memory: only unattended runs read or write it
# --------------------------------------------------------------------------- #


def _load_memory():
    """``agents/memory.py`` with the AgentCore SDK stubbed; only the gate is tested."""
    config_mod = "bedrock_agentcore.memory.integrations.strands.config"
    manager_mod = "bedrock_agentcore.memory.integrations.strands.session_manager"
    for name in (
        "bedrock_agentcore",
        "bedrock_agentcore.memory",
        "bedrock_agentcore.memory.integrations",
        "bedrock_agentcore.memory.integrations.strands",
        config_mod,
        manager_mod,
    ):
        sys.modules.setdefault(name, types.ModuleType(name))

    class Recorder:
        built: list = []

        def __init__(self, **kwargs):
            Recorder.built.append(kwargs)

    sys.modules[config_mod].AgentCoreMemoryConfig = lambda **kw: kw
    sys.modules[config_mod].RetrievalConfig = lambda **kw: kw
    sys.modules[manager_mod].AgentCoreMemorySessionManager = Recorder
    return _load("scope_memory", AGENTS / "memory.py"), Recorder


memory, MemoryRecorder = _load_memory()


@pytest.mark.parametrize("trigger", ["chat"])
def test_a_human_run_never_reads_or_writes_memory(monkeypatch, trigger):
    """Chat is a human's words, and an approval execution (also trigger=chat) acts
    with a Manager's authority. Neither may share memory with runs of another
    authority: that is how a front-desk user plants text a Manager's run follows."""
    monkeypatch.setenv("MEMORY_ID", "hotel_ops-test")
    ctx = run_context.from_payload(
        {"propertyId": HOTEL_A, "trigger": trigger, "operatingDate": "2026-01-01"}
    )
    MemoryRecorder.built.clear()
    assert memory.build_session_manager("billing", ctx) is None
    assert MemoryRecorder.built == []


@pytest.mark.parametrize("trigger", ["schedule", "event"])
def test_an_unattended_run_gets_memory_private_to_its_run(monkeypatch, trigger):
    monkeypatch.setenv("MEMORY_ID", "hotel_ops-test")
    ctx = run_context.from_payload(
        {"propertyId": HOTEL_A, "trigger": trigger, "operatingDate": "2026-01-01"}
    )
    MemoryRecorder.built.clear()
    assert memory.build_session_manager("night_audit", ctx) is not None
    config = MemoryRecorder.built[0]["agentcore_memory_config"]
    assert ctx.run_id in config["session_id"]



# --------------------------------------------------------------------------- #
# A human with no groups has no authority -- never the agents' full authority
# --------------------------------------------------------------------------- #


def test_a_chat_run_always_sends_its_groups_even_when_there_are_none():
    """An empty tuple used to omit the header, which read as "no human asked"."""
    run_context.set_current(
        run_context.from_payload(
            {"propertyId": HOTEL_A, "trigger": "chat", "callerGroups": [],
             "operatingDate": "2026-01-01"}
        )
    )
    assert gateway.correlation_headers("arrivals")["X-Hotel-Ops-Caller-Groups"] == ""


def test_a_payload_with_no_trigger_is_treated_as_a_human_run():
    """Fail closed: the default must never be the one with the agents' authority."""
    ctx = run_context.from_payload({"propertyId": HOTEL_A, "operatingDate": "2026-01-01"})
    assert ctx.trigger == "chat"


def test_a_human_with_no_groups_can_call_nothing():
    response = run(call("billing___list_folios", {"propertyId": HOTEL_A}), groups=[])
    assert refused(response)["code"] == "NOT_PERMITTED_FOR_CALLER"


def test_a_chat_run_that_arrives_without_its_groups_header_fails_closed():
    """Malformed, so treated as a human with no authority, not as unattended."""
    event = {
        "mcp": {
            "gatewayRequest": {
                "body": call("arrivals___assign_room", {"propertyId": HOTEL_A}),
                "headers": {"X-Hotel-Ops-Property-Id": HOTEL_A, "X-Hotel-Ops-Trigger": "chat"},
            }
        }
    }
    assert refused(interceptor.handler(event, None))["code"] == "NOT_PERMITTED_FOR_CALLER"


def test_an_unattended_run_still_uses_the_agents_own_identity():
    event = {
        "mcp": {
            "gatewayRequest": {
                "body": call("arrivals___assign_room", {"propertyId": HOTEL_A}),
                "headers": {"X-Hotel-Ops-Property-Id": HOTEL_A, "X-Hotel-Ops-Trigger": "schedule"},
            }
        }
    }
    assert refused(interceptor.handler(event, None)) is None


# --------------------------------------------------------------------------- #
# The synth-time check on the platform's console app client
# --------------------------------------------------------------------------- #

sys.path.insert(0, str(REPO / "infra"))
from stacks.foundation_config import FoundationConfig, writable_scope_attributes  # noqa: E402


class FakeCognito:
    def __init__(self, writable):
        self.writable = writable
        self.calls = []

    def describe_user_pool_client(self, **kwargs):
        self.calls.append(kwargs)
        return {"UserPoolClient": {"WriteAttributes": self.writable}}


def _foundation():
    return FoundationConfig(
        crs_api_url="https://crs.test", pms_api_url="https://pms.test",
        user_pool_id="us-east-1_TEST", admin_auth_client_id="admin",
        user_pool_client_id="spa",
    )


def test_the_check_reports_the_platforms_writable_scope_attributes():
    """The public platform's SPA client writes both, which is the finding."""
    fake = FakeCognito(["email", "custom:guest_id", "custom:property_id", "custom:region"])
    assert writable_scope_attributes(_foundation(), region="us-east-1", cognito=fake) == [
        "custom:property_id",
        "custom:region",
    ]
    # It asks about the client the console signs staff in with, and nothing else.
    assert fake.calls == [{"UserPoolId": "us-east-1_TEST", "ClientId": "spa"}]


def test_a_client_that_cannot_write_scope_passes():
    fake = FakeCognito(["email", "given_name", "family_name"])
    assert writable_scope_attributes(_foundation(), region="us-east-1", cognito=fake) == []
