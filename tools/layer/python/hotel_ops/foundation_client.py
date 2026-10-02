# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Authenticated HTTP client for the hospitality foundation's CRS/PMS APIs.

A port of ``src/pms/activity_simulator/simulator.py``'s ``_load_creds`` /
``_get_token`` / ``_api_call``, which is the foundation's own precedent for a
Lambda calling its APIs as a Cognito user. Kept deliberately close to that
original: same ``{"status", "data", "ok"}`` return shape, same pacing sleep, same
one-shot reactive 401 refresh.

Three rules from ``PATTERN_EXTENSION_GUIDE.md`` §3.2 are enforced here rather than left to
each handler:

1. **The foundation's response envelope is returned verbatim.** Errors are never
   paraphrased -- the model reasons better on ``{"success": false, "error":
   {"code","message","details"}}`` than on our summary of it.
2. **The database is never touched.** There is no RDS, no Data API, no psycopg
   import in this layer. Every read and write is an HTTP call.
3. **Outbound calls are paced.** The foundation's WAF is in COUNT mode today but
   the rate limits are real.

Identity
--------
The agent's Cognito username comes from ``AGENT_USERNAME_TEMPLATE``. For the four
chain-level agents that is a literal address. For the housekeeping agent it
contains ``{property_id}``, because ``verify_property_access`` requires
``custom:property_id`` for a group that is neither chain-level nor regional --
so there is one user per property and the caller must say which one it wants.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Literal

import boto3

Surface = Literal["crs", "pms"]

#: Refresh this many seconds before the token actually expires, so a long
#: handler cannot start a request with a token that dies mid-flight.
TOKEN_REFRESH_BUFFER_SECONDS = 60

HTTP_TIMEOUT_SECONDS = 25


class FoundationError(RuntimeError):
    """A transport- or auth-level failure. Not an API error envelope."""


def is_conflict(result: dict) -> bool:
    """A ``409`` means someone else already did it -- a correct outcome.

    ``PATTERN_EXTENSION_GUIDE.md`` §3.2: a human front-desk agent getting there first is
    success by someone else, not a failure and not a retry trigger.
    """
    return result.get("status") == 409


class FoundationClient:
    """One agent's authenticated view of the foundation APIs.

    Instantiate once at Lambda module scope so the token cache survives warm
    invocations.
    """

    def __init__(
        self,
        *,
        crs_api_url: str | None = None,
        pms_api_url: str | None = None,
        user_pool_id: str | None = None,
        admin_auth_client_id: str | None = None,
        creds_secret_arn: str | None = None,
        username_template: str | None = None,
        pacing_seconds: float | None = None,
    ) -> None:
        env = os.environ
        self._urls: dict[str, str] = {
            "crs": (crs_api_url or env["CRS_API_URL"]).rstrip("/"),
            "pms": (pms_api_url or env["PMS_API_URL"]).rstrip("/"),
        }
        self._user_pool_id = user_pool_id or env["COGNITO_USER_POOL_ID"]
        self._client_id = admin_auth_client_id or env["ADMIN_AUTH_CLIENT_ID"]
        self._secret_arn = creds_secret_arn or env["AGENT_CREDS_SECRET_ARN"]
        self._username_template = username_template or env["AGENT_USERNAME_TEMPLATE"]
        self._pacing = (
            pacing_seconds
            if pacing_seconds is not None
            else int(env.get("API_PACING_MS", "50")) / 1000.0
        )

        self._cognito = None
        self._secrets = None
        self._password: str | None = None
        # username -> (id_token, expires_at). A property-scoped agent holds one
        # entry per property it has been asked about.
        self._tokens: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    @property
    def is_property_scoped(self) -> bool:
        return "{property_id}" in self._username_template

    def username_for(self, property_id: str | None) -> str:
        """Resolve which Cognito user this call signs in as."""
        if not self.is_property_scoped:
            return self._username_template
        if not property_id:
            raise FoundationError(
                "This agent has one Cognito identity per property, so every call "
                "must supply propertyId. Its Cognito group is not chain-level, so "
                "the foundation would reject a token without custom:property_id."
            )
        return self._username_template.format(property_id=property_id)

    def _get_password(self) -> str:
        """Read the shared password once per warm container."""
        if self._password is None:
            if self._secrets is None:
                self._secrets = boto3.client("secretsmanager")
            secret = json.loads(
                self._secrets.get_secret_value(SecretId=self._secret_arn)[
                    "SecretString"
                ]
            )
            self._password = secret["password"]
        return self._password

    def token(
        self, *, property_id: str | None = None, force_refresh: bool = False
    ) -> str:
        """A Cognito **ID** token for this agent.

        ID token, not access token: ``cognito:groups`` and ``custom:property_id``
        -- the only two claims the foundation authorizes on -- appear nowhere
        else.
        """
        username = self.username_for(property_id)
        now = time.time()

        if not force_refresh:
            cached = self._tokens.get(username)
            if cached and now < (cached[1] - TOKEN_REFRESH_BUFFER_SECONDS):
                return cached[0]

        if self._cognito is None:
            self._cognito = boto3.client("cognito-idp")

        response = self._cognito.admin_initiate_auth(
            UserPoolId=self._user_pool_id,
            ClientId=self._client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": self._get_password(),
            },
        )
        result = response.get("AuthenticationResult")
        if not result:
            raise FoundationError(
                f"Cognito returned challenge {response.get('ChallengeName')!r} for "
                f"{username} instead of tokens. The agent user's password is not "
                "marked Permanent."
            )

        self._tokens[username] = (result["IdToken"], now + result["ExpiresIn"])
        return result["IdToken"]

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def call(
        self,
        method: str,
        surface: Surface,
        path: str,
        *,
        body: dict | None = None,
        query: dict[str, Any] | None = None,
        property_id: str | None = None,
    ) -> dict:
        """Single source of truth for outbound HTTP.

        Returns ``{"status": int, "data": <parsed envelope>, "ok": bool}``. The
        ``data`` field is the foundation's envelope, unmodified.
        """
        try:
            base = self._urls[surface]
        except KeyError:
            raise ValueError(f"unknown API surface {surface!r}; use 'crs' or 'pms'")

        url = f"{base}/{path.lstrip('/')}"
        if query:
            # Drop None so callers can pass optional filters unconditionally.
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned)}"

        token = self.token(property_id=property_id)
        status, raw = self._request(method, url, body, token)

        if status == 401:
            token = self.token(property_id=property_id, force_refresh=True)
            status, raw = self._request(method, url, body, token)
            if status == 401:
                raise FoundationError(
                    f"Still 401 after a forced token refresh: {method} {url}"
                )

        # Pace *after* the call, matching the foundation simulator, so a burst of
        # tool calls in one handler cannot outrun the WAF's rate limit.
        if self._pacing:
            time.sleep(self._pacing)

        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.decode("utf-8", errors="replace")

        return {"status": status, "data": parsed, "ok": 200 <= status < 300}

    def _request(
        self, method: str, url: str, body: dict | None, token: str
    ) -> tuple[int, bytes]:
        scheme = urllib.parse.urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise FoundationError(f"Disallowed URL scheme {scheme!r} for {url!r}")

        request = urllib.request.Request(
            url=url,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                # The foundation logs this; it makes an agent's call traceable in
                # the platform's own logs, not just ours.
                "X-Correlation-Id": str(uuid.uuid4()),
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(  # nosec B310 - scheme validated above
                request, timeout=HTTP_TIMEOUT_SECONDS
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # Non-2xx is normal: the envelope in the body is what the model needs.
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise FoundationError(f"{method} {url} failed: {exc.reason}") from exc

    # ------------------------------------------------------------------ #
    # Convenience wrappers
    # ------------------------------------------------------------------ #

    def get(self, surface: Surface, path: str, **kwargs) -> dict:
        return self.call("GET", surface, path, **kwargs)

    def post(self, surface: Surface, path: str, **kwargs) -> dict:
        return self.call("POST", surface, path, **kwargs)

    def put(self, surface: Surface, path: str, **kwargs) -> dict:
        return self.call("PUT", surface, path, **kwargs)
