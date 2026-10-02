# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /properties``, with the hotel platform replaced.

The console API had no offline tests at all before this file -- roughly 700 lines
across three Lambdas, including the approval-release path, covered only by the live
Layer 4 check. This is the start of closing that, and it starts here because this
function is the one console Lambda that talks to the platform, and its whole
correctness rests on one thing: it must forward *the caller's own* token and never
substitute any other authority. A version of this that quietly called the platform
unauthenticated, or with a service credential, would return the entire chain's
property list to a housekeeper and no test of the happy path would notice.

So the cases below are mostly about the token and about what happens when the
platform says no. Fully offline: ``urlopen`` is replaced, so nothing leaves the
process.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from staff_registry import REGISTRY, register_as_claimed

REPO = Path(__file__).resolve().parents[2]
LAMBDAS = REPO / "infra" / "lambdas"
CONSOLE_LAYER = LAMBDAS / "console" / "layer" / "python"

PMS_URL = "https://pms.example.test/dev"
TOKEN = "eyJraWQ6-a-perfectly-ordinary-id-token"  # nosec B105 - test fake

AUSTIN = {
    "propertyId": "a1a1a1a1-0000-4000-8000-000000000001",
    "name": "AnyCompany Bay Austin Hotel & Spa",
    "city": "Austin",
    "state": "TX",
    "region": None,
}
BEND = {
    "propertyId": "b2b2b2b2-0000-4000-8000-000000000002",
    "name": "AnyCompany Bay Bend Residences",
    "city": "Bend",
    "state": "OR",
    "region": None,
}


@pytest.fixture()
def module(monkeypatch):
    """Import the handler with the environment it reads at module scope."""
    monkeypatch.setenv("PMS_API_URL", PMS_URL)
    if str(CONSOLE_LAYER) not in sys.path:
        sys.path.insert(0, str(CONSOLE_LAYER))
    spec = importlib.util.spec_from_file_location(
        "console_properties_index", LAMBDAS / "console" / "properties" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def event(
    *, claims: dict | None = None, headers: dict | None = None, register: bool = True
) -> dict:
    claims = {
        "sub": "u-1",
        "email": "gm@anycompany.test",
        # A staff group by default: a caller in none is refused outright.
        "cognito:groups": "Manager",
        **(claims or {}),
    }
    if register:
        # Registered with exactly the scope the token claims. The tests that are
        # about the registry pass register=False and arrange it themselves.
        register_as_claimed(claims)
    return {
        "requestContext": {"authorizer": {"claims": claims}},
        "headers": {"Authorization": TOKEN} if headers is None else headers,
    }


class FakePlatform:
    """Stands in for ``urlopen``. Records the request it was handed."""

    def __init__(self, payload: dict, *, raises: Exception | None = None):
        self.payload = payload
        self.raises = raises
        self.request = None

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.request = request
        self.timeout = timeout
        if self.raises:
            raise self.raises
        body = json.dumps(self.payload).encode()

        class Response(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return Response(body)


def body_of(response: dict) -> dict:
    return json.loads(response["body"])


def install(monkeypatch, module, fake):
    # Patched on urllib itself: the call now lives in the shared
    # hotel_console.platform helper, which looks urlopen up at call time.
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


# --------------------------------------------------------------------------- #
# The token. This is the whole security model of this function.
# --------------------------------------------------------------------------- #
def test_the_callers_own_token_is_what_reaches_the_platform(monkeypatch, module):
    fake = install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN]}})
    )

    module.handler(event(), None)

    assert fake.request.full_url == f"{PMS_URL}/properties"
    # Verbatim. Not re-signed, not swapped for an agent's credential, not stripped.
    assert fake.request.get_header("Authorization") == TOKEN


def test_a_lowercase_authorization_header_is_still_forwarded(monkeypatch, module):
    """A browser may send either casing and API Gateway preserves what it got.

    Matching only the capitalised spelling would have produced a 500 for some clients
    and worked for others, which is the least debuggable outcome available.
    """
    fake = install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN]}})
    )

    module.handler(event(headers={"authorization": TOKEN}), None)

    assert fake.request.get_header("Authorization") == TOKEN


def test_a_missing_authorization_header_fails_instead_of_calling_the_platform(
    monkeypatch, module
):
    """The one failure that must never degrade into a successful request.

    Calling the platform with no token would either 401 (harmless) or, if the
    platform's authorizer were ever relaxed, return every property to a caller whose
    scope was never checked. Refuse locally instead.
    """
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {}}))

    response = module.handler(event(headers={}), None)

    assert response["statusCode"] == 500
    assert body_of(response)["error"]["code"] == "MISSING_AUTHORIZATION"
    assert fake.request is None, "the platform must not be called without a token"


def test_a_request_with_no_verified_claims_is_refused(monkeypatch, module):
    """If the Cognito authorizer is ever detached, stop working loudly."""
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {}}))

    response = module.handler({"headers": {"Authorization": TOKEN}}, None)

    assert response["statusCode"] == 401
    assert fake.request is None


# --------------------------------------------------------------------------- #
# What the console does with the answer
# --------------------------------------------------------------------------- #
def test_the_platforms_property_list_is_passed_through_unchanged(monkeypatch, module):
    install(
        monkeypatch,
        module,
        FakePlatform({"success": True, "data": {"properties": [AUSTIN, BEND]}}),
    )

    data = body_of(module.handler(event(), None))["data"]

    assert data["properties"] == [AUSTIN, BEND]


def test_a_scoped_user_is_flagged_so_the_console_offers_no_choice(monkeypatch, module):
    """A housekeeper gets one property and a label, not a dropdown of one.

    The flag comes from the caller's own claim rather than from counting rows,
    because a chain-level manager at a one-hotel chain is a different case: they may
    still legitimately ask chain-wide.
    """
    install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN]}})
    )

    data = body_of(
        module.handler(
            event(claims={"custom:property_id": AUSTIN["propertyId"]}), None
        )
    )["data"]

    assert data["scope"]["boundToOneProperty"] is True


def test_a_chain_level_caller_is_not_flagged_as_bound(monkeypatch, module):
    install(
        monkeypatch,
        module,
        FakePlatform({"success": True, "data": {"properties": [AUSTIN, BEND]}}),
    )

    data = body_of(module.handler(event(), None))["data"]

    assert data["scope"]["boundToOneProperty"] is False
    assert data["scope"]["region"] is None


def test_a_regional_callers_region_is_reported(monkeypatch, module):
    install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [BEND]}})
    )

    data = body_of(module.handler(event(claims={"custom:region": "WEST"}), None))["data"]

    assert data["scope"]["region"] == "WEST"


def test_an_empty_property_list_is_not_an_error(monkeypatch, module):
    """The platform returns ``{"properties": []}`` for a caller in no useful group.

    That is a real answer about their access, not a fault, and the console shows it as
    "no property access" rather than as an outage.
    """
    install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": []}})
    )

    response = module.handler(event(), None)

    assert response["statusCode"] == 200
    assert body_of(response)["data"]["properties"] == []


# --------------------------------------------------------------------------- #
# When the platform says no
# --------------------------------------------------------------------------- #
def test_a_platform_403_is_returned_verbatim_rather_than_paraphrased(monkeypatch, module):
    """Same rule the tool layer follows: return the platform's own envelope.

    "Access denied: insufficient permissions" tells an operator something true about
    their account. "Could not load properties" tells them nothing.
    """
    refusal = json.dumps(
        {
            "success": False,
            "error": {
                "code": "FORBIDDEN",
                "message": "Access denied: insufficient permissions",
            },
        }
    ).encode()
    install(
        monkeypatch,
        module,
        FakePlatform(
            {},
            raises=urllib.error.HTTPError(
                f"{PMS_URL}/properties", 403, "Forbidden", {}, io.BytesIO(refusal)
            ),
        ),
    )

    response = module.handler(event(), None)

    assert response["statusCode"] == 403
    error = body_of(response)["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["message"] == "Access denied: insufficient permissions"


def test_a_platform_error_with_an_unreadable_body_still_keeps_its_status(
    monkeypatch, module
):
    install(
        monkeypatch,
        module,
        FakePlatform(
            {},
            raises=urllib.error.HTTPError(
                f"{PMS_URL}/properties", 502, "Bad Gateway", {}, io.BytesIO(b"<html>")
            ),
        ),
    )

    response = module.handler(event(), None)

    assert response["statusCode"] == 502
    assert body_of(response)["error"]["code"] == "PLATFORM_ERROR"


def test_an_unreachable_platform_is_a_502_and_says_so(monkeypatch, module):
    install(
        monkeypatch,
        module,
        FakePlatform({}, raises=urllib.error.URLError("connection refused")),
    )

    response = module.handler(event(), None)

    assert response["statusCode"] == 502
    assert body_of(response)["error"]["code"] == "PLATFORM_UNREACHABLE"


def test_a_reply_without_a_properties_list_fails_loudly(monkeypatch, module):
    """A shape change in the platform must not render as an empty picker.

    An empty picker reads as "you have no properties", which is a wrong and quietly
    misleading answer. A 502 reads as "this is broken", which is the true one.
    """
    install(monkeypatch, module, FakePlatform({"success": True, "data": {}}))

    response = module.handler(event(), None)

    assert response["statusCode"] == 502
    assert body_of(response)["error"]["code"] == "UNEXPECTED_PLATFORM_SHAPE"


def test_the_platform_call_is_bounded_well_inside_the_function_timeout(
    monkeypatch, module
):
    """The function has 10s; a slow platform must surface as our 502, not a 504."""
    fake = install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": []}})
    )

    module.handler(event(), None)

    assert 0 < fake.timeout < 10


def test_a_non_http_platform_url_is_refused_before_anything_is_opened(monkeypatch, module):
    """urllib honours file://, so a misconfigured PMS_API_URL would otherwise read a
    local file into an API response. Refused before any request is built."""
    monkeypatch.setenv("PMS_API_URL", "file:///etc/passwd")
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {}}))

    response = module.handler(event(), None)

    assert response["statusCode"] == 500
    assert body_of(response)["error"]["code"] == "MISCONFIGURED"
    assert fake.request is None, "nothing may be opened"


def test_a_caller_in_no_staff_group_is_refused_before_the_platform_is_asked(
    monkeypatch, module
):
    """The security finding's entry point. The platform's pool also holds guest
    accounts, and a guest can write their own custom:property_id through the SPA
    client -- so a groupless, property-bound caller must not get past the console."""
    fake = install(
        monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN]}})
    )

    response = module.handler(
        event(claims={"cognito:groups": "", "custom:property_id": AUSTIN["propertyId"]}),
        None,
    )

    assert response["statusCode"] == 403
    assert body_of(response)["error"]["code"] == "NOT_STAFF"
    assert fake.request is None


# --------------------------------------------------------------------------- #
# Scope comes from the registry, never from the token's own claims
# --------------------------------------------------------------------------- #
#
# The platform's SPA app client lets a signed-in user rewrite their own
# custom:property_id and custom:region, so the token says whatever the user last set.
# These pin the registry as the authority.


def test_a_staff_member_who_is_not_registered_is_refused(monkeypatch, module):
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {"properties": []}}))

    response = module.handler(event(register=False), None)

    assert response["statusCode"] == 403
    assert body_of(response)["error"]["code"] == "NOT_REGISTERED"
    assert fake.request is None


def test_rewriting_your_own_property_attribute_gets_you_nothing(monkeypatch, module):
    """Registered at Austin; token now claims Bend. Refused, not silently corrected."""
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [BEND]}}))
    REGISTRY["u-1"] = {"sub": "u-1", "propertyId": AUSTIN["propertyId"], "region": None}

    response = module.handler(
        event(claims={"custom:property_id": BEND["propertyId"]}, register=False), None
    )

    assert response["statusCode"] == 403
    assert body_of(response)["error"]["code"] == "SCOPE_MISMATCH"
    assert fake.request is None


def test_deleting_your_property_attribute_does_not_make_you_chain_level(monkeypatch, module):
    """The escalation a check on rewritten values alone would miss: a property-scoped
    Manager clears custom:property_id, and without the registry would read as a
    chain-level Manager who may see every hotel."""
    fake = install(monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN, BEND]}}))
    REGISTRY["u-1"] = {"sub": "u-1", "propertyId": AUSTIN["propertyId"], "region": None}

    response = module.handler(event(register=False), None)  # token has no property

    assert response["statusCode"] == 403
    assert body_of(response)["error"]["code"] == "SCOPE_MISMATCH"
    assert fake.request is None


def test_rewriting_your_own_region_is_refused_too(monkeypatch, module):
    install(monkeypatch, module, FakePlatform({"success": True, "data": {"properties": []}}))
    REGISTRY["u-1"] = {"sub": "u-1", "propertyId": None, "region": "WEST"}

    response = module.handler(
        event(claims={"cognito:groups": "RegionalManager", "custom:region": "EAST"},
              register=False),
        None,
    )

    assert body_of(response)["error"]["code"] == "SCOPE_MISMATCH"


def test_a_registered_scope_that_matches_the_token_is_used(monkeypatch, module):
    install(monkeypatch, module, FakePlatform({"success": True, "data": {"properties": [AUSTIN]}}))
    REGISTRY["u-1"] = {"sub": "u-1", "propertyId": AUSTIN["propertyId"], "region": None}

    response = module.handler(
        event(claims={"custom:property_id": AUSTIN["propertyId"]}, register=False), None
    )

    assert response["statusCode"] == 200
    assert body_of(response)["data"]["scope"]["boundToOneProperty"] is True
