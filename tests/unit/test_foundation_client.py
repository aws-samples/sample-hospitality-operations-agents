# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Token caching, identity resolution, and the 409-is-success rule.

Nothing here touches AWS or the network: ``boto3`` clients and
``urllib.request.urlopen`` are both replaced with fakes, so the suite runs
offline and cannot reach the live foundation by accident.

The token-cache tests matter more than they look. A property-scoped agent holds
one token per property in a single warm container; if the cache were keyed on
anything but the resolved username, the housekeeping agent would silently reuse
property A's token for property B and the foundation would answer with property
A's data. That is a tenancy bug that no amount of prompt engineering could fix.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from hotel_ops.foundation_client import (
    TOKEN_REFRESH_BUFFER_SECONDS,
    FoundationClient,
    FoundationError,
    is_conflict,
)

CHAIN_TEMPLATE = "agent-billing@anycompany.internal"
SCOPED_TEMPLATE = "agent-housekeeping+{property_id}@anycompany.internal"


class FakeCognito:
    """Mimics ``admin_initiate_auth``, counting calls and issuing unique tokens."""

    def __init__(self, *, expires_in: int = 3600, challenge: str | None = None):
        self.calls: list[str] = []
        self._expires_in = expires_in
        self._challenge = challenge

    def admin_initiate_auth(self, **kwargs):
        username = kwargs["AuthParameters"]["USERNAME"]
        self.calls.append(username)
        if self._challenge:
            return {"ChallengeName": self._challenge}
        return {
            "AuthenticationResult": {
                "IdToken": f"id-token-for-{username}-{len(self.calls)}",
                "AccessToken": "access-token-which-must-never-be-used",
                "ExpiresIn": self._expires_in,
            }
        }


class FakeSecrets:
    def __init__(self, password: str = "s3cret"):  # nosec B107 - test fake
        self.calls = 0
        self._password = password

    def get_secret_value(self, **_kwargs):
        self.calls += 1
        return {"SecretString": json.dumps({"username": "x", "password": self._password})}


def make_client(template: str = CHAIN_TEMPLATE, **overrides) -> FoundationClient:
    client = FoundationClient(
        crs_api_url="https://crs.example/dev/",
        pms_api_url="https://pms.example/dev",
        user_pool_id="us-east-1_TEST",
        admin_auth_client_id="client",
        creds_secret_arn="arn:aws:secretsmanager:us-east-1:1:secret:x",  # nosec B106 - fake ARN; pragma: allowlist secret (fake ARN)
        username_template=template,
        pacing_seconds=0,  # no sleeping in tests
    )
    client._cognito = overrides.get("cognito", FakeCognito())
    client._secrets = overrides.get("secrets", FakeSecrets())
    return client


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #


def test_a_chain_level_agent_has_one_identity():
    client = make_client(CHAIN_TEMPLATE)
    assert client.is_property_scoped is False
    # propertyId is a request filter for these agents, not an identity selector.
    assert client.username_for(None) == CHAIN_TEMPLATE
    assert client.username_for("any-property") == CHAIN_TEMPLATE


def test_a_property_scoped_agent_resolves_one_identity_per_property():
    client = make_client(SCOPED_TEMPLATE)
    assert client.is_property_scoped is True
    assert (
        client.username_for("prop-a")
        == "agent-housekeeping+prop-a@anycompany.internal"
    )


def test_a_property_scoped_agent_refuses_to_guess_which_property():
    """Housekeeping is in neither CHAIN_LEVEL_GROUPS nor REGIONAL_GROUPS, so a
    token without ``custom:property_id`` is rejected by every task endpoint.
    Failing here gives a message the model can act on instead of a 403."""
    client = make_client(SCOPED_TEMPLATE)
    with pytest.raises(FoundationError, match="must supply propertyId"):
        client.username_for(None)


# --------------------------------------------------------------------------- #
# Token cache
# --------------------------------------------------------------------------- #


def test_the_id_token_is_used_not_the_access_token():
    """Only the ID token carries cognito:groups and custom:property_id."""
    client = make_client()
    assert client.token().startswith("id-token-for-")


def test_a_warm_container_reuses_its_token():
    cognito = FakeCognito()
    client = make_client(cognito=cognito)
    first = client.token()
    assert client.token() == first
    assert len(cognito.calls) == 1


def test_the_password_is_read_from_secrets_manager_only_once():
    secrets = FakeSecrets()
    client = make_client(secrets=secrets)
    client.token()
    client.token(force_refresh=True)
    assert secrets.calls == 1


def test_a_token_is_refreshed_before_it_expires_not_after():
    """Refresh must happen inside the buffer, so a request cannot start with a
    token that dies mid-flight."""
    cognito = FakeCognito(expires_in=TOKEN_REFRESH_BUFFER_SECONDS + 10)
    client = make_client(cognito=cognito)
    client.token()
    assert len(cognito.calls) == 1

    # Advance to inside the refresh buffer without waiting in real time.
    username, (token, expires_at) = next(iter(client._tokens.items()))
    client._tokens[username] = (token, expires_at - 11)

    client.token()
    assert len(cognito.calls) == 2, "expected a refresh once inside the buffer"


def test_each_property_gets_its_own_cache_entry():
    """The tenancy-critical case: one warm container, two properties."""
    cognito = FakeCognito()
    client = make_client(SCOPED_TEMPLATE, cognito=cognito)

    token_a = client.token(property_id="prop-a")
    token_b = client.token(property_id="prop-b")

    assert token_a != token_b
    assert "prop-a" in token_a and "prop-b" in token_b
    assert len(cognito.calls) == 2
    # And both are now cached independently.
    assert client.token(property_id="prop-a") == token_a
    assert len(cognito.calls) == 2


def test_a_password_that_is_not_permanent_fails_with_a_diagnosable_message():
    client = make_client(cognito=FakeCognito(challenge="NEW_PASSWORD_REQUIRED"))
    with pytest.raises(FoundationError, match="NEW_PASSWORD_REQUIRED"):
        client.token()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def patch_http(monkeypatch, *responses):
    """Queue one or more responses; records every request that was made."""
    sent: list[dict] = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        sent.append(
            {
                "url": request.full_url,
                "method": request.method,
                # urllib capitalizes header names on the way in ("X-correlation-id"),
                # so normalize rather than assert against its casing.
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": json.loads(request.data) if request.data else None,
            }
        )
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "err", {}, io.BytesIO(body)
            )
        return FakeResponse(status, body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return sent


def envelope(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def test_a_successful_call_returns_the_envelope_untouched(monkeypatch):
    body = {"success": True, "data": {"stays": []}, "metadata": {"page": 1}}
    patch_http(monkeypatch, (200, envelope(body)))
    result = make_client().get("pms", "/stays")
    assert result == {"status": 200, "data": body, "ok": True}


def test_an_error_envelope_is_returned_verbatim_not_paraphrased(monkeypatch):
    """§4.3: the model reasons better on the real error than on our summary."""
    body = {
        "success": False,
        "error": {"code": "INVALID_STATE", "message": "task is COMPLETED", "details": {}},
    }
    patch_http(monkeypatch, (409, envelope(body)))
    result = make_client().post("pms", "/housekeeping/tasks/1/complete")
    assert result["data"] == body
    assert result["ok"] is False


def test_409_is_recognised_as_success_by_someone_else(monkeypatch):
    """A front-desk human getting there first is a correct outcome, not a retry
    trigger (§4.3)."""
    patch_http(monkeypatch, (409, envelope({"success": False})))
    result = make_client().put("pms", "/stays/1/assign-room", body={"roomId": "r"})
    assert is_conflict(result) is True
    assert is_conflict({"status": 200}) is False


def test_the_base_url_trailing_slash_does_not_produce_a_double_slash(monkeypatch):
    sent = patch_http(monkeypatch, (200, envelope({"success": True})))
    make_client().get("crs", "/properties/p/room-types")
    assert sent[0]["url"] == "https://crs.example/dev/properties/p/room-types"


def test_none_query_values_are_dropped_so_handlers_can_pass_filters_blindly(monkeypatch):
    sent = patch_http(monkeypatch, (200, envelope({"success": True})))
    make_client().get(
        "pms", "/stays", query={"propertyId": "p", "status": None, "limit": 50}
    )
    assert sent[0]["url"] == "https://pms.example/dev/stays?propertyId=p&limit=50"


def test_every_request_carries_the_bearer_token_and_a_correlation_id(monkeypatch):
    sent = patch_http(monkeypatch, (200, envelope({"success": True})))
    make_client().get("pms", "/properties")
    headers = sent[0]["headers"]
    assert headers["authorization"].startswith("Bearer id-token-for-")
    # Makes an agent's call traceable in the foundation's own logs, not just ours.
    assert len(headers["x-correlation-id"]) == 36


def test_a_401_triggers_exactly_one_forced_refresh_then_retries(monkeypatch):
    cognito = FakeCognito()
    client = make_client(cognito=cognito)
    sent = patch_http(
        monkeypatch, (401, b'{"message":"Unauthorized"}'), (200, envelope({"success": True}))
    )
    result = client.get("pms", "/properties")
    assert result["ok"] is True
    assert len(sent) == 2
    assert len(cognito.calls) == 2
    assert sent[0]["headers"]["authorization"] != sent[1]["headers"]["authorization"]


def test_a_persistent_401_raises_rather_than_looping(monkeypatch):
    patch_http(monkeypatch, (401, b"{}"))
    with pytest.raises(FoundationError, match="Still 401"):
        make_client().get("pms", "/properties")


def test_a_transport_failure_is_distinguishable_from_an_api_error(monkeypatch):
    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(FoundationError, match="name resolution failed"):
        make_client().get("pms", "/properties")


def test_an_unknown_api_surface_is_a_programming_error(monkeypatch):
    with pytest.raises(ValueError, match="unknown API surface"):
        make_client().get("analytics", "/whatever")


def test_a_non_json_body_is_returned_as_text_rather_than_crashing(monkeypatch):
    """A WAF block page or a gateway timeout is HTML, and the model should see it."""
    patch_http(monkeypatch, (503, b"<html>Service Unavailable</html>"))
    result = make_client().get("pms", "/properties")
    assert result["data"] == "<html>Service Unavailable</html>"
    assert result["ok"] is False


def test_pacing_sleeps_after_the_call_to_stay_under_the_waf_rate_limit(monkeypatch):
    slept: list[float] = []
    patch_http(monkeypatch, (200, envelope({"success": True})))
    monkeypatch.setattr("hotel_ops.foundation_client.time.sleep", slept.append)
    client = make_client()
    client._pacing = 0.05
    client.get("pms", "/properties")
    assert slept == [0.05]
