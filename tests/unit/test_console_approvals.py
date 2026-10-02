# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The approval queue: where a human releases money movement.

The first offline tests this function has had. Until now it was covered only by the
live Layer 4 check, and two things the threat model found were in it:

* **Anyone who could approve could approve their own proposal.** An approver filed a
  proposal and released it, and the record showed one person's decision twice.
* **The released token was written into the execution prompt**, so the model saw it,
  AgentCore Memory stored it, traces recorded it, and on the first real approval the
  model repeated it in its answer. It now rides in the payload's ``approvalToken``
  field, which the Runtime turns into a Gateway header the model never reads.

DynamoDB and SQS are replaced; nothing leaves the process.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from staff_registry import register_as_claimed

REPO = Path(__file__).resolve().parents[2]
CONSOLE = REPO / "infra" / "lambdas" / "console"

PROPERTY = "a1a1a1a1-0000-4000-8000-000000000001"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("CHAT_QUEUE_URL", "https://sqs.test/chat")
    monkeypatch.setenv("APPROVALS_TABLE", "approvals")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if str(CONSOLE / "layer" / "python") not in sys.path:
        sys.path.insert(0, str(CONSOLE / "layer" / "python"))
    spec = importlib.util.spec_from_file_location(
        "console_approvals_index", CONSOLE / "approvals" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTable:
    def __init__(self, items: list[dict]):
        self.items = items
        self.updates: list[dict] = []

    def scan(self, **kwargs):
        wanted = kwargs["ExpressionAttributeValues"][":p"]
        return {"Items": [i for i in self.items if i.get("proposalId") == wanted]}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}


class FakeQueue:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(json.loads(kwargs["MessageBody"]))
        return {}


def pending(proposed_by: str = "filer@anycompany.test") -> dict:
    return {
        "approvalId": "apv-the-secret-token-value",
        "proposalId": "prop-1",
        "status": "PENDING",
        "action": "post_charge",
        "folioId": "f1",
        "amount": 40,
        "description": "Late checkout",
        "propertyId": PROPERTY,
        "reason": "The guest left at 3pm.",
        "proposedBy": proposed_by,
    }


def approve_as(module, monkeypatch, item: dict, *, email: str, groups="Manager"):
    fake_table, queue = FakeTable([item]), FakeQueue()
    monkeypatch.setattr(module, "table", lambda _name: fake_table)
    monkeypatch.setattr(module, "_sqs", queue)
    event = {
        "httpMethod": "POST",
        "resource": "/approvals/{id}/approve",
        "pathParameters": {"id": "prop-1"},
        "body": json.dumps({"note": "Checked against the folio."}),
        "requestContext": {
            "authorizer": {"claims": {"sub": f"sub-{email}", "email": email,
                                      "cognito:groups": groups}}
        },
    }
    register_as_claimed(event["requestContext"]["authorizer"]["claims"])
    response = module.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), queue, fake_table


def test_an_approver_cannot_release_their_own_proposal(module, monkeypatch):
    status, body, queue, table = approve_as(
        module, monkeypatch, pending("gm@anycompany.test"), email="gm@anycompany.test"
    )
    assert status == 403
    assert body["error"]["code"] == "SELF_APPROVAL"
    assert queue.sent == [], "nothing may be dispatched"
    assert table.updates == [], "and the proposal stays PENDING"


def test_a_second_person_can(module, monkeypatch):
    status, body, queue, _ = approve_as(
        module, monkeypatch, pending("filer@anycompany.test"), email="gm@anycompany.test"
    )
    assert status == 200 and body["data"]["status"] == "APPROVED"
    assert len(queue.sent) == 1


def test_the_released_token_rides_out_of_band_and_never_in_the_prompt(module, monkeypatch):
    _, _, queue, _ = approve_as(module, monkeypatch, pending(), email="gm@anycompany.test")
    message = queue.sent[0]
    assert message["approvalToken"] == "apv-the-secret-token-value"
    assert "apv-the-secret-token-value" not in message["prompt"]


def test_the_execution_run_is_pinned_to_the_approvals_property(module, monkeypatch):
    """The Gateway pins every call to this, so it must be the approval's own."""
    _, _, queue, _ = approve_as(module, monkeypatch, pending(), email="gm@anycompany.test")
    message = queue.sent[0]
    assert message["propertyId"] == PROPERTY
    assert f"propertyId={PROPERTY}" in message["prompt"]


def test_no_approval_response_ever_carries_the_token(module, monkeypatch):
    _, body, _, _ = approve_as(module, monkeypatch, pending(), email="gm@anycompany.test")
    assert "apv-the-secret-token-value" not in json.dumps(body)
