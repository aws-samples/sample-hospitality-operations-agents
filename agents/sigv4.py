# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SigV4 request signing for the AgentCore Gateway's MCP endpoint.

The Gateway's inbound authorizer is ``GatewayAuthorizer.using_aws_iam()``, so the
Runtime's own execution role is the caller's identity. That choice removes an
entire class of operational work -- no second Cognito pool for machine-to-machine
auth, no client secret to rotate, no token endpoint round trip before the first
tool call -- but it means the MCP transport has to sign each HTTP request rather
than attach a bearer token.

``strands.tools.mcp.MCPClient`` accepts an ``httpx.Auth``, which is exactly the
hook needed: httpx hands us the fully-formed request, we sign it, and the
streamable-HTTP transport is otherwise untouched.
"""

from __future__ import annotations

import logging
from typing import Iterator

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)

#: The signing name for the AgentCore data plane. Not ``bedrock``.
SERVICE_NAME = "bedrock-agentcore"

#: Headers SigV4 produces that must be copied onto the outgoing request.
_SIGNED_HEADERS = (
    "Authorization",
    "X-Amz-Date",
    "X-Amz-Security-Token",
    "X-Amz-Content-SHA256",
)


class SigV4Signer(httpx.Auth):
    """Signs every request with the caller's current AWS credentials."""

    #: httpx must buffer the body before calling us: SigV4 hashes the payload,
    #: so signing a request whose body we have not read produces a signature the
    #: service rejects with an opaque 403.
    requires_request_body = True

    def __init__(self, region: str, session: boto3.Session | None = None):
        self._region = region
        self._session = session or boto3.Session()
        self._credentials = self._session.get_credentials()
        if self._credentials is None:
            raise RuntimeError(
                "No AWS credentials are available to sign Gateway requests. "
                "Inside AgentCore Runtime these come from the execution role."
            )

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        # Resolve on every request rather than once at construction. The runtime
        # container can live for hours; a frozen copy of a role's temporary
        # credentials would start failing partway through a long night-audit run.
        frozen = self._credentials.get_frozen_credentials()

        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            # Content-Type participates in the canonical request for some
            # services and is stable here, so sign it rather than leave the
            # signature dependent on transport defaults.
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
        )
        SigV4Auth(frozen, SERVICE_NAME, self._region).add_auth(aws_request)

        for header in _SIGNED_HEADERS:
            value = aws_request.headers.get(header)
            if value is not None:
                request.headers[header] = value

        yield request
