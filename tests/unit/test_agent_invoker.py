# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The queue-backed Runtime invoker, with the Runtime and DynamoDB replaced.

This function is the only thing standing between a schedule firing and an agent
writing to the foundation, and every one of its failure modes happens at 3 a.m.
with nobody watching. So the cases here are the unattended ones: a stream that
ends early, a Runtime that reports an error, a batch where one message is bad, and
a summary write that fails after the run already did its work.

Fully offline. ``boto3`` clients are constructed at import, so the module is
imported once with the environment set and both clients are replaced per test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[2] / "infra" / "lambdas"

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/hotel_ops_agent-abc"


def _load():
    """Import the handler with the environment it reads at module scope."""
    import os

    os.environ["RUNTIME_ARN"] = RUNTIME_ARN
    os.environ["ENDPOINT_NAME"] = "production"
    os.environ["DECISIONS_TABLE"] = "hotel-ops-agent-decisions"
    # The bedrock-agentcore client is built at import, as it is in Lambda, where
    # AWS_REGION is always present. Set here so the suite does not depend on the
    # developer's ambient AWS config -- and never reaches a real endpoint: both
    # clients are replaced before any test runs.
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    spec = importlib.util.spec_from_file_location(
        "agent_invoker_index", LAMBDAS / "agent_invoker" / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


invoker = _load()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeStream:
    """A ``StreamingBody`` that yields SSE lines, as bytes, like botocore does."""

    def __init__(self, events: list[dict] | None = None, *, raw: bytes | None = None):
        self._lines = (
            [b"data: " + json.dumps(e).encode() for e in events] if events else []
        )
        self._raw = raw

    def iter_lines(self):
        # Interleaved blanks and an SSE comment: the real transport sends both, and
        # a parser that chokes on them fails only against the live service.
        for line in self._lines:
            yield b""
            yield b": keep-alive"
            yield line

    def read(self):
        return self._raw or b""


class FakeRuntime:
    def __init__(self, events=None, *, raw=None, content_type="text/event-stream",
                 error: Exception | None = None):
        self.events = events
        self.raw = raw
        self.content_type = content_type
        self.error = error
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {
            "contentType": self.content_type,
            "response": FakeStream(self.events, raw=self.raw),
        }


class FakeDynamo:
    def __init__(self, *, fail: bool = False):
        self.puts: list[dict] = []
        self.fail = fail

    def put_item(self, **kwargs):
        if self.fail:
            raise RuntimeError("ProvisionedThroughputExceededException")
        self.puts.append(kwargs)
        return {}


@pytest.fixture
def wired(monkeypatch):
    """Replace both clients and hand back the fakes."""

    def _wire(events=None, **kwargs):
        runtime = FakeRuntime(events, **kwargs)
        dynamo = FakeDynamo()
        monkeypatch.setattr(invoker, "_AGENTCORE", runtime)
        monkeypatch.setattr(invoker, "_dynamodb", dynamo)
        return runtime, dynamo

    return _wire


def message(payload: dict, message_id: str = "m-1") -> dict:
    return {"messageId": message_id, "body": json.dumps(payload)}


def sqs_event(*payloads: dict) -> dict:
    return {
        "Records": [message(p, f"m-{i}") for i, p in enumerate(payloads, start=1)]
    }


def completed(text: str = "Assigned room 1010.", **extra) -> dict:
    return {"type": "run_completed", "text": text, "stopReason": "end_turn", **extra}


def item_of(dynamo: FakeDynamo, index: int = 0) -> dict:
    return dynamo.puts[index]["Item"]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_a_scheduled_run_invokes_the_named_endpoint_and_records_its_answer(wired):
    runtime, dynamo = wired(
        [
            {"type": "run_started", "runId": "r-1"},
            {"type": "delegation", "agent": "arrivals_agent"},
            {"type": "text", "delta": "Assigned "},
            {"type": "text", "delta": "room 1010."},
            completed("Assigned room 1010.", usage={"totalTokens": 4200}),
        ]
    )

    response = invoker.handler(
        sqs_event(
            {
                "prompt": "Pre-assign rooms.",
                "propertyId": "p-1",
                "operatingDate": "2026-09-09",
                # A real uuid4, not a short label. The earlier version of this test
                # used "run-abc" and asserted it reached the Runtime verbatim, which
                # is precisely the bug that shipped: AgentCore rejects a session id
                # under 33 characters, so a short run id failed every invocation.
                # See the session-id tests at the end of this file.
                "runId": "b1b0e5d2-0000-4000-8000-000000000001",
                "trigger": "schedule",
            }
        ),
        None,
    )

    assert response == {"batchItemFailures": []}

    call = runtime.calls[0]
    assert call["agentRuntimeArn"] == RUNTIME_ARN
    # Never DEFAULT: an unattended run must execute the same build a human tested.
    assert call["qualifier"] == "production"
    # The session id is the run id, so the Runtime's session and the decision log's
    # partition key are the same string.
    assert call["runtimeSessionId"] == "b1b0e5d2-0000-4000-8000-000000000001"
    assert json.loads(call["payload"])["prompt"] == "Pre-assign rooms."

    item = item_of(dynamo)
    assert item["run_id"] == {"S": "b1b0e5d2-0000-4000-8000-000000000001"}
    assert item["agent"] == {"S": "orchestrator"}
    assert item["kind"] == {"S": "run_summary"}
    assert item["recommendation"] == {"S": "Assigned room 1010."}
    assert item["outcome"] == {"S": "ok"}
    assert item["property_id"] == {"S": "p-1"}
    assert item["operating_date"] == {"S": "2026-09-09"}
    assert item["delegations"] == {"L": [{"S": "arrivals_agent"}]}
    assert item["stop_reason"] == {"S": "end_turn"}
    assert item["usage"] == {"M": {"totalTokens": {"N": "4200"}}}
    assert "duration_seconds" in item


def test_the_summary_sorts_after_the_tool_calls_the_run_made(wired):
    """The interceptor writes `{iso}#{hex}` per tool call; the conclusion is last."""
    _, dynamo = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "runId": "r"}), None)
    event_id = item_of(dynamo)["event_id"]["S"]
    timestamp, _, suffix = event_id.partition("#")
    assert suffix == "zz-summary"
    # Same partition key as the tool rows, so one Query returns the whole run.
    assert item_of(dynamo)["run_id"] == {"S": "r"}
    assert item_of(dynamo)["ts"] == {"S": timestamp}


def test_the_full_text_of_the_completion_event_wins_over_accumulated_deltas(wired):
    """A dropped frame must not produce an answer with an invisible hole in it."""
    _, dynamo = wired(
        [
            {"type": "text", "delta": "partial"},
            completed("the whole answer"),
        ]
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": "the whole answer"}


def test_deltas_are_used_when_the_completion_event_carries_no_text(wired):
    _, dynamo = wired(
        [
            {"type": "text", "delta": "a"},
            {"type": "text", "delta": "b"},
            {"type": "run_completed", "stopReason": "end_turn"},
        ]
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": "ab"}


# --------------------------------------------------------------------------- #
# Payload defaults
# --------------------------------------------------------------------------- #


def test_the_operating_date_is_stamped_at_delivery_not_left_to_the_runtime(wired):
    """A schedule cannot carry a date -- it would be the deploy date forever -- and
    the row and the run must agree on which day was reasoned over."""
    runtime, dynamo = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go"}), None)

    sent = json.loads(runtime.calls[0]["payload"])["operatingDate"]
    assert sent == item_of(dynamo)["operating_date"]["S"]
    assert len(sent) == 10 and sent.count("-") == 2


def test_a_run_id_is_generated_when_the_trigger_did_not_supply_one(wired):
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    run_id = json.loads(runtime.calls[0]["payload"])["runId"]
    # 36 characters, which is also the reason no padding is needed for AgentCore's
    # 33-character minimum on the session id.
    assert len(run_id) == 36
    assert runtime.calls[0]["runtimeSessionId"] == run_id


def test_the_trigger_defaults_to_schedule_not_chat(wired):
    """`chat` would tell the orchestrator a human is waiting, and it would ask a
    clarifying question nobody will ever answer."""
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert json.loads(runtime.calls[0]["payload"])["trigger"] == "schedule"


def test_a_blank_property_id_is_dropped_rather_than_sent_empty(wired):
    """A5 is chain-wide. An empty string would log as a property that does not
    exist, instead of as the chain."""
    runtime, dynamo = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "propertyId": "   "}), None)
    assert "propertyId" not in json.loads(runtime.calls[0]["payload"])
    assert item_of(dynamo)["property_id"] == {"S": "_chain"}


# --------------------------------------------------------------------------- #
# Failure, which is the whole point of the queue
# --------------------------------------------------------------------------- #


def test_a_run_that_reports_an_error_is_recorded_and_then_retried(wired):
    runtime, dynamo = wired(
        [
            {"type": "error", "code": "INVALID_PAYLOAD", "message": "no prompt"},
            completed("ignored"),
        ]
    )
    response = invoker.handler(sqs_event({"prompt": "go"}), None)

    # Reported to SQS, so the message is redriven and eventually lands in the DLQ
    # where a human can see it. Absorbing it would make a broken schedule look
    # like a working one.
    assert response == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
    # And recorded anyway: a run that vanished and a run that failed look identical
    # from the console otherwise.
    assert item_of(dynamo)["outcome"] == {"S": "failed"}
    assert "INVALID_PAYLOAD" in item_of(dynamo)["error_code"]["S"]


def test_a_stream_that_ends_without_run_completed_is_a_failure(wired):
    """The agent may have written to the foundation and then been cut off. That is
    not a successful run, and reporting it as one would hide a real problem."""
    _, dynamo = wired([{"type": "text", "delta": "half an ans"}])
    response = invoker.handler(sqs_event({"prompt": "go"}), None)

    assert response["batchItemFailures"] == [{"itemIdentifier": "m-1"}]
    assert item_of(dynamo)["outcome"] == {"S": "failed"}
    assert "run_completed" in item_of(dynamo)["error_code"]["S"]
    # The partial text is kept: it is evidence of how far the run got.
    assert item_of(dynamo)["recommendation"] == {"S": "half an ans"}


def test_an_empty_answer_is_recorded_as_such_rather_than_as_a_blank_row(wired):
    _, dynamo = wired([completed("")])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": "(the agent produced no text)"}
    assert item_of(dynamo)["outcome"] == {"S": "ok"}


def test_one_bad_message_does_not_fail_its_siblings(wired):
    """Batch size is 1 today. This is the assertion that keeps that a tuning knob
    rather than a correctness dependency."""
    runtime, dynamo = wired([completed()])
    event = sqs_event({"prompt": "first"}, {"prompt": "third"})
    event["Records"].insert(1, {"messageId": "m-bad", "body": "{not json"})

    response = invoker.handler(event, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "m-bad"}]}
    assert len(runtime.calls) == 2, "the good messages must still run"


def test_a_message_with_no_prompt_is_failed_before_the_runtime_is_invoked(wired):
    runtime, _ = wired([completed()])
    response = invoker.handler(sqs_event({"propertyId": "p-1"}), None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "m-1"}]
    assert runtime.calls == [], "refused after paying for a model turn"


def test_an_invocation_error_is_reported_and_not_swallowed(wired, monkeypatch):
    runtime, dynamo = wired([completed()], error=RuntimeError("ThrottlingException"))
    response = invoker.handler(sqs_event({"prompt": "go"}), None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "m-1"}]
    # No summary row: the run never started, so there is nothing to summarize.
    assert dynamo.puts == []


def test_a_failed_summary_write_never_fails_the_run(wired, monkeypatch):
    """The run already did its work. Losing the paperwork must not cause a redrive
    that repeats the writes."""
    runtime, _ = wired([completed()])
    monkeypatch.setattr(invoker, "_dynamodb", FakeDynamo(fail=True))
    assert invoker.handler(sqs_event({"prompt": "go"}), None) == {
        "batchItemFailures": []
    }


def test_the_runtime_client_never_retries_by_itself():
    """A botocore retry would start a second agent run, duplicating writes the
    first one may already have made. Retries belong to the queue, where a redrive
    is visible and bounded.

    Asserted on the *normalized* config: botocore turns ``max_attempts=0`` into
    ``total_max_attempts=1``, so reading back the key that was written would pass
    while telling you nothing.
    """
    config = invoker._AGENTCORE.meta.config
    assert config.retries["total_max_attempts"] == 1
    # And the read timeout must outlast a real run; measured runs reach 210s, and
    # botocore's 60s default would sever a working one.
    assert config.read_timeout > 600


# --------------------------------------------------------------------------- #
# Storage limits and stream shapes
# --------------------------------------------------------------------------- #


def test_a_runaway_answer_is_truncated_rather_than_lost(wired, monkeypatch):
    """A rejected 400 KB PutItem would lose the whole row. A truncated
    recommendation is still evidence."""
    monkeypatch.setattr(invoker, "MAX_RECOMMENDATION_CHARS", 50)
    _, dynamo = wired([completed("x" * 500)])
    invoker.handler(sqs_event({"prompt": "go"}), None)

    stored = item_of(dynamo)["recommendation"]["S"]
    assert stored.startswith("x" * 50)
    assert "[truncated for storage]" in stored
    # Flagged, so nobody reads a cut-off sentence as the agent's conclusion.
    assert item_of(dynamo)["recommendation_truncated"] == {"BOOL": True}


def test_blank_lines_and_sse_comments_do_not_break_the_parser(wired):
    """FakeStream interleaves both, because the real transport does."""
    _, dynamo = wired([completed("fine")])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": "fine"}


def test_an_unparseable_frame_is_skipped_not_fatal(wired, monkeypatch):
    runtime = FakeRuntime([completed("survived")])
    dynamo = FakeDynamo()
    monkeypatch.setattr(invoker, "_AGENTCORE", runtime)
    monkeypatch.setattr(invoker, "_dynamodb", dynamo)
    original = FakeStream.iter_lines

    def with_garbage(self):
        yield b"data: {not json"
        yield from original(self)

    monkeypatch.setattr(FakeStream, "iter_lines", with_garbage)
    assert invoker.handler(sqs_event({"prompt": "go"}), None)["batchItemFailures"] == []
    assert item_of(dynamo)["recommendation"] == {"S": "survived"}


def test_a_non_streaming_response_is_still_read(wired):
    """``main.py`` streams today. A future entrypoint returning a plain dict must
    not read as an empty run -- silently, and only on the unattended path."""
    _, dynamo = wired(
        raw=json.dumps([completed("json mode")]).encode(),
        content_type="application/json",
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": "json mode"}


def test_delegation_order_is_preserved_because_it_is_the_routing_decision(wired):
    _, dynamo = wired(
        [
            {"type": "delegation", "agent": "night_audit_agent"},
            {"type": "delegation", "agent": "billing_agent"},
            completed(),
        ]
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["delegations"] == {
        "L": [{"S": "night_audit_agent"}, {"S": "billing_agent"}]
    }


def test_nothing_is_written_when_the_decisions_table_is_unset(wired, monkeypatch):
    """The same degradation the interceptors have: no table, no write, no failure."""
    _, dynamo = wired([completed()])
    monkeypatch.setattr(invoker, "DECISIONS_TABLE", "")
    assert invoker.handler(sqs_event({"prompt": "go"}), None) == {
        "batchItemFailures": []
    }
    assert dynamo.puts == []


# --------------------------------------------------------------------------- #
# The session id, which a caller's run id must not be able to invalidate
# --------------------------------------------------------------------------- #
#
# AgentCore rejects a runtimeSessionId under 33 characters at parameter validation,
# before the request is sent. The invoker used to pass the run id through unchanged,
# reasoning that a uuid4 is 36 -- true of the ids it generates, false of the ones its
# callers supply. The ops console mints `exec-<16>` = 21 characters, so every
# approved Tier-2 write died here: a human approved a charge, the token was minted,
# and the execution run silently never happened.


def test_a_short_caller_supplied_run_id_is_padded_not_passed_through(wired):
    """The regression. This exact id shape comes from the approval route."""
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "runId": "exec-_sNrbMqQ2aO4W4na"}), None)

    session = runtime.calls[0]["runtimeSessionId"]
    assert len(session) >= invoker.MIN_SESSION_ID
    # The run id stays a visible prefix, so a human reading the two side by side can
    # still see they are the same run.
    assert session.startswith("exec-_sNrbMqQ2aO4W4na")


def test_a_qualifying_run_id_is_used_unchanged(wired):
    """Equal strings mean the Runtime session and the decision log's run_id can be
    followed across both without a mapping table."""
    runtime, dynamo = wired([completed()])
    run_id = "b1b0e5d2-0000-4000-8000-000000000001"
    invoker.handler(sqs_event({"prompt": "go", "runId": run_id}), None)
    assert runtime.calls[0]["runtimeSessionId"] == run_id
    assert item_of(dynamo)["run_id"] == {"S": run_id}


def test_padding_is_deterministic_so_a_retry_reuses_the_session(wired):
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "runId": "short"}), None)
    invoker.handler(sqs_event({"prompt": "go", "runId": "short"}), None)
    assert runtime.calls[0]["runtimeSessionId"] == runtime.calls[1]["runtimeSessionId"]


def test_the_session_id_stays_within_the_upper_bound(wired):
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "runId": "x" * 90}), None)
    assert len(runtime.calls[0]["runtimeSessionId"]) <= 100


@pytest.mark.parametrize("run_id", ["a", "exec-1", "x" * 32])
def test_every_undersized_id_becomes_valid(wired, run_id):
    runtime, _ = wired([completed()])
    invoker.handler(sqs_event({"prompt": "go", "runId": run_id}), None)
    assert len(runtime.calls[0]["runtimeSessionId"]) >= invoker.MIN_SESSION_ID


# --------------------------------------------------------------------------- #
# Approval tokens must not survive the run
# --------------------------------------------------------------------------- #
#
# A released token has to reach the model -- the model writes tool arguments and
# approval_token is one -- but it must not end up in the audit log. Two paths put it
# there and both were live: the execution prompt contains it by construction, and on
# the first real end-to-end approval the model repeated it in its own answer, which
# GET /runs/{id} then served to any operator who could read the run, including the
# non-approvers who had just been refused the authority to mint one.

# Synthetic, but the real shape: the prefix and a long urlsafe body, which
# is what the redaction pattern has to match.
LIVE_TOKEN = "apv-SYNTHETIC-test-token-not-a-real-approval0"  # nosec B105 - synthetic; pragma: allowlist secret


def test_a_token_the_model_echoed_is_not_stored(wired):
    """The regression, using the exact token shape that leaked."""
    _, dynamo = wired(
        [completed(f"The charge was not posted. Your token {LIVE_TOKEN} was passed.")]
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)

    stored = item_of(dynamo)["recommendation"]["S"]
    assert LIVE_TOKEN not in stored
    assert "apv-[redacted]" in stored
    # The rest of the sentence survives: the answer is still the audit record.
    assert "The charge was not posted." in stored


def test_the_token_in_the_execution_prompt_is_not_stored_either(wired):
    """The prompt is stored so an operator can see what was asked. The approval
    route's prompt contains the token by construction."""
    _, dynamo = wired([completed("Done.")])
    invoker.handler(
        sqs_event(
            {
                "prompt": (
                    "A human approved your proposal to post_charge on folioId f1, "
                    f"amount 41.5. Execute it passing approval_token={LIVE_TOKEN} "
                    "to the tool."
                )
            }
        ),
        None,
    )
    stored = item_of(dynamo)["prompt"]["S"]
    assert LIVE_TOKEN not in stored
    assert "approval_token=apv-[redacted]" in stored
    # And the instruction is still legible, so the run remains auditable.
    assert "post_charge on folioId f1" in stored


def test_several_tokens_in_one_answer_are_all_redacted(wired):
    _, dynamo = wired(
        [completed(f"first {LIVE_TOKEN} then apv-{'B' * 30} done")]
    )
    invoker.handler(sqs_event({"prompt": "go"}), None)
    stored = item_of(dynamo)["recommendation"]["S"]
    assert "apv-" in stored
    assert stored.count("apv-[redacted]") == 2
    assert LIVE_TOKEN not in stored and "B" * 30 not in stored


def test_ordinary_prose_is_untouched(wired):
    """Redaction must not mangle an answer that has no token in it."""
    answer = "Approved the charge on folio 00000000-0000-0000-0000-000000000000."
    _, dynamo = wired([completed(answer)])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    assert item_of(dynamo)["recommendation"] == {"S": answer}


def test_redaction_happens_before_the_length_cap(wired, monkeypatch):
    """Truncating first could leave a partial token, which is not a credential but
    is also not something an audit log should be storing fragments of."""
    monkeypatch.setattr(invoker, "MAX_RECOMMENDATION_CHARS", 40)
    _, dynamo = wired([completed(f"{LIVE_TOKEN} and then a lot more text " * 5)])
    invoker.handler(sqs_event({"prompt": "go"}), None)
    stored = item_of(dynamo)["recommendation"]["S"]
    assert "apv-FRxH" not in stored
