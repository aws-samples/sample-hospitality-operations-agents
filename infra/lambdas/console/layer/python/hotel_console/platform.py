# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reads from the hotel platform, **as the operator** -- never as anyone else.

The console's own Lambdas have no platform identity. When one of them needs to ask
the platform something, it forwards the operator's own ID token, so the platform's
authorizer answers for *that human*: a property-scoped operator sees their property's
records and nothing else, exactly as they would in the platform's own UI. Nothing
here holds a credential, caches a reply, or re-implements a scoping rule; the
platform is the only authority, the same principle the agents' tool layer follows.

``GET /properties`` uses it to pass the platform's property list through. It lives
in the layer rather than in that one function because any console route that has to
check something against the platform should do it this way, as the caller, and not
grow a second copy of the forwarding and error handling.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from hotel_console.api import ApiError

#: Well under the functions' 10s timeout, so a slow platform surfaces as our error
#: rather than as API Gateway's opaque 504.
TIMEOUT_SECONDS = 6


def authorization(event: dict) -> str:
    """The caller's raw ID token, as they sent it.

    API Gateway preserves the client's header casing, and a browser may send either
    spelling, so both are checked. This is the one piece of the request that must be
    forwarded untouched: it is what makes the platform scope the reply to this human.
    """
    headers = (event or {}).get("headers") or {}
    for key, value in headers.items():
        if key.lower() == "authorization" and value:
            return value
    # The Cognito authorizer would have rejected the request before this, so reaching
    # here means the authorizer is detached or the identity source was changed.
    raise ApiError(
        500,
        "MISSING_AUTHORIZATION",
        "No Authorization header reached this function, so the caller's token "
        "cannot be forwarded to the platform. Check the Cognito authorizer's "
        "identity source on this method.",
    )


def get_as_caller(event: dict, path: str) -> dict:
    """``GET {PMS_API_URL}{path}`` with the caller's token; the envelope on 2xx.

    A platform error is raised as an :class:`ApiError` carrying the platform's own
    status, code and message -- a 403 here is a real answer about this operator's
    access, and the console should show what the platform actually said.
    """
    base = os.environ["PMS_API_URL"].rstrip("/")
    # urllib honours file:// and any registered custom scheme. PMS_API_URL is deploy
    # config, not caller input, so this is not reachable from a request today -- but a
    # misconfigured value would read a local file into an API response, and the check
    # costs nothing. Same guard, same reason, as foundation_client's _request.
    scheme = urllib.parse.urlparse(base).scheme
    if scheme not in ("http", "https"):
        raise ApiError(
            500, "MISCONFIGURED", f"PMS_API_URL has disallowed scheme {scheme!r}."
        )
    request = urllib.request.Request(
        f"{base}{path}",
        headers={"Authorization": authorization(event), "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - scheme validated above
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        raise ApiError(
            exc.code,
            (error or {}).get("code") or "PLATFORM_ERROR",
            (error or {}).get("message")
            or f"The hotel platform refused this request ({exc.code}).",
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError(
            502, "PLATFORM_UNREACHABLE", f"Could not reach the hotel platform: {exc}"
        ) from exc
