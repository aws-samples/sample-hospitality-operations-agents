# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared machinery for the three ops-console API functions.

Pure standard library plus the boto3 the Lambda runtime already ships, so the
layer needs no build step and no vendored wheels -- unlike ``agents/``, which
needs arm64 dependency bundling.

The one idea worth stating up front: **this layer never trusts a request body for
identity.** Every function reads who the caller is, which groups they hold, and
which property they are scoped to from ``requestContext.authorizer.claims``, which
API Gateway populates only after Cognito has verified the ID token's signature. A
body-supplied ``propertyId`` may narrow a caller's scope but can never widen it,
and ``approvedBy`` is always the token's subject. That asymmetry is what makes the
approval queue meaningful: a human's click is only worth something if it is
provably that human's.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Callable

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: Groups the foundation treats as chain-level. Copied from
#: ``src/layers/common/utils/tenant.py`` rather than re-derived: the console must
#: not grant a scope the foundation's own authorizer would refuse, or an operator
#: sees runs for a property whose data they cannot actually open.
CHAIN_LEVEL_GROUPS = frozenset({"Admin", "Manager", "RevenueManager"})
REGIONAL_GROUPS = frozenset({"RegionalManager"})

#: Every staff group the platform defines. A caller in none of them is not staff --
#: the platform's user pool also holds *guest* accounts, from its booking site -- and
#: is refused by every route. Without this, a groupless caller with a
#: ``custom:property_id`` passed :meth:`Caller.scope`, and its chat run reached the
#: Gateway with an empty group list that read as "no human asked", so a hotel guest
#: could drive Manager-level agents. A security review found that.
STAFF_GROUPS = frozenset(
    {"Admin", "Manager", "RevenueManager", "RegionalManager", "FrontDesk", "Housekeeping"}
)

#: Who may approve money movement. Deliberately narrower than "can read the
#: console": FrontDesk and Housekeeping staff can watch every run and override a
#: judgment, and cannot release a charge.
APPROVER_GROUPS = frozenset({"Admin", "Manager"})


class ApiError(Exception):
    """An error with an HTTP status. Raised anywhere, rendered once."""

    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Caller:
    """The authenticated human, read only from verified token claims."""

    def __init__(self, claims: dict):
        self.claims = claims
        self.subject: str = claims.get("sub") or ""
        self.email: str = claims.get("email") or claims.get("cognito:username") or ""
        raw_groups = claims.get("cognito:groups") or ""
        # API Gateway flattens a list claim to a comma-separated string, but a
        # direct Lambda test event may carry the list. Both are handled because the
        # difference is invisible until an authorization check silently passes.
        if isinstance(raw_groups, str):
            self.groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
        else:
            self.groups = tuple(str(g) for g in raw_groups)
        self.property_id: str | None = claims.get("custom:property_id") or None
        self.region: str | None = claims.get("custom:region") or None

    @property
    def is_chain_level(self) -> bool:
        return bool(set(self.groups) & (CHAIN_LEVEL_GROUPS | REGIONAL_GROUPS))

    @property
    def may_approve(self) -> bool:
        return bool(set(self.groups) & APPROVER_GROUPS)

    def scope(self, requested: str | None) -> str | None:
        """The property this request may read, or ``None`` for chain-wide.

        A caller pinned to one property by ``custom:property_id`` cannot ask for
        another, and asking is an error rather than a silent substitution -- a
        console that quietly showed you your own hotel when you asked for a
        different one would be worse than one that refused.
        """
        requested = (requested or "").strip() or None
        if self.property_id:
            if requested and requested != self.property_id:
                raise ApiError(
                    403,
                    "OUT_OF_SCOPE",
                    f"Your account is scoped to property {self.property_id} and "
                    f"cannot read {requested}.",
                )
            return self.property_id
        if not self.is_chain_level:
            raise ApiError(
                403,
                "NO_PROPERTY_ACCESS",
                "Your account has no property scope and no chain-level group, so "
                "there is nothing it can read. This mirrors the foundation's own "
                "verify_property_access.",
            )
        return requested

    def require_approver(self, action: str) -> None:
        if not self.may_approve:
            raise ApiError(
                403,
                "NOT_AN_APPROVER",
                f"Releasing {action} requires one of {sorted(APPROVER_GROUPS)}; you "
                f"hold {list(self.groups) or 'no groups'}. Reading runs and "
                "recording an override do not require it -- approving money does.",
            )


def caller_of(event: dict) -> Caller:
    """The caller, from claims API Gateway has already verified.

    Raises rather than defaulting to an anonymous caller. If the authorizer is ever
    detached the functions must stop working loudly, not start serving every run in
    the company to anyone who can reach the URL.
    """
    authorizer = ((event or {}).get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims")
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise ApiError(
            401,
            "UNAUTHENTICATED",
            "No verified Cognito claims on this request. The Cognito authorizer "
            "must be attached to every method on this API.",
        )
    caller = Caller(claims)
    if not set(caller.groups) & STAFF_GROUPS:
        raise ApiError(
            403,
            "NOT_STAFF",
            "This console is for hotel staff. Your account is in no staff group, so "
            "it cannot use it.",
        )
    _apply_registered_scope(caller)
    return caller


def registered_scope(subject: str) -> dict | None:
    """The caller's scope as an administrator registered it, or ``None``.

    Read from ``hotel-ops-agent-staff-scope``, which only
    ``scripts/register_staff_scope.py`` writes, with AWS IAM credentials. A module
    function so tests can replace it.
    """
    item = table("STAFF_SCOPE_TABLE").get_item(Key={"sub": subject}).get("Item")
    return item or None


def _apply_registered_scope(caller: "Caller") -> None:
    """Replace the token's scope claims with the registered scope, or refuse.

    The token's ``custom:property_id`` and ``custom:region`` cannot be trusted on
    their own: the platform's SPA app client lets a signed-in user rewrite both, so
    they say whatever the user last set. A security review found that. The platform
    has no staff table to check them against, and this project may not change its
    app client, so the authority lives here instead -- in a registry a Cognito user
    has no way to write.

    Groups are still read from the token, deliberately: a user cannot change their
    own group membership, which takes the administrator APIs.

    A claim that *disagrees* with the registry is refused rather than silently
    corrected. Correcting it would hide an attempt to widen scope; refusing it makes
    the operator ask an administrator, and makes the mismatch visible in the logs.
    The reverse case matters as much as the obvious one: a property-scoped Manager
    who *deletes* their custom:property_id would otherwise look chain-level.
    """
    scope = registered_scope(caller.subject)
    if scope is None:
        logger.warning("unregistered caller sub=%s", caller.subject)
        raise ApiError(
            403,
            "NOT_REGISTERED",
            "Your account is not registered for this console. An administrator "
            "registers operators with scripts/register_staff_scope.py.",
        )
    registered_property = scope.get("propertyId") or None
    registered_region = scope.get("region") or None
    if (caller.property_id, caller.region) != (registered_property, registered_region):
        logger.warning(
            "scope mismatch sub=%s token=(%s,%s) registered=(%s,%s)",
            caller.subject,
            caller.property_id,
            caller.region,
            registered_property,
            registered_region,
        )
        raise ApiError(
            403,
            "SCOPE_MISMATCH",
            "Your account's property or region attribute does not match the scope an "
            "administrator registered for you, so the console will not use either. "
            "Ask an administrator to check your registration.",
        )
    caller.property_id = registered_property
    caller.region = registered_region


def body_of(event: dict) -> dict:
    raw = (event or {}).get("body")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "INVALID_JSON", f"Request body is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ApiError(400, "INVALID_BODY", "Request body must be a JSON object.")
    return parsed


def handler_for(route: Callable[[dict, Caller], Any]) -> Callable[[dict, Any], dict]:
    """Wrap a route function with claim extraction and error rendering.

    Every response is the foundation's own envelope shape -- ``{"success", "data"}``
    or ``{"success", "error"}`` -- so the console consumes one shape whether the
    data came from our API or was passed through from the hotel platform.
    """

    def handler(event: dict, context: Any) -> dict:
        try:
            caller = caller_of(event)
            data = route(event, caller)
            return _response(200, {"success": True, "data": data})
        except ApiError as exc:
            logger.warning("api error %s %s: %s", exc.status, exc.code, exc.message)
            return _response(
                exc.status,
                {
                    "success": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Logged in full, returned in outline. An operator needs to know the
            # request failed; the stack trace belongs in CloudWatch.
            logger.exception("unhandled error")
            return _response(
                500,
                {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"{type(exc).__name__}. See CloudWatch for detail.",
                    },
                },
            )

    return handler


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        # No CORS headers anywhere in this API. The console is served from the same
        # CloudFront distribution that fronts this API under /api/*, so every
        # request is same-origin and a preflight never happens. That is a deliberate
        # alternative to the wildcard CORS the foundation carries.
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_encode),
    }


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        # DynamoDB numbers arrive as Decimal. int() where exact so a token count
        # does not render as 4200.0.
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


# --------------------------------------------------------------------------- #
# DynamoDB
# --------------------------------------------------------------------------- #

_resources: dict[str, Any] = {}


def table(name_env: str):
    """A ``Table`` resource, cached per environment variable.

    The resource API rather than the client: these functions read and write
    ordinary Python values, and hand-rolling ``{"S": ...}`` in three handlers is
    how a type mismatch reaches production. The interceptors use the client API
    because they run on the hot path and care about the wire form.
    """
    if name_env not in _resources:
        name = os.environ.get(name_env)
        if not name:
            raise ApiError(
                500, "MISCONFIGURED", f"{name_env} is not set on this function."
            )
        _resources[name_env] = boto3.resource("dynamodb").Table(name)
    return _resources[name_env]


def path_param(event: dict, name: str) -> str:
    value = ((event or {}).get("pathParameters") or {}).get(name)
    if not value:
        raise ApiError(400, "MISSING_PATH_PARAMETER", f"{name} is required in the path.")
    return value


def query_param(event: dict, name: str, default: str | None = None) -> str | None:
    params = (event or {}).get("queryStringParameters") or {}
    value = params.get(name)
    return value if value not in (None, "") else default


def int_param(event: dict, name: str, default: int, *, maximum: int) -> int:
    raw = query_param(event, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "INVALID_PARAMETER", f"{name} must be an integer.") from exc
    if value < 1:
        raise ApiError(400, "INVALID_PARAMETER", f"{name} must be at least 1.")
    return min(value, maximum)
