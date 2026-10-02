# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: provision the agents' Cognito identities.

Creates the dedicated Cognito *users* the agent tool Lambdas sign in as. Nothing
else about the foundation's user pool is touched: no pool settings, no groups, no
clients, no schema.

Why users and not an M2M client
-------------------------------
The foundation authorizes on ``cognito:groups`` and ``custom:property_id``
(``src/layers/common/utils/tenant.py``). Those claims appear only in a Cognito
user ID token; an OAuth client-credentials token has neither. So each agent needs
a real user, signed in via ``ADMIN_USER_PASSWORD_AUTH``.

Two identity shapes, forced by ``verify_property_access``
--------------------------------------------------------
``verify_property_access`` has two branches. When ``custom:property_id`` is unset
it grants access only to ``CHAIN_LEVEL_GROUPS`` (Admin, Manager, RevenueManager)
and ``REGIONAL_GROUPS`` (RegionalManager); everyone else is denied outright. When
``custom:property_id`` *is* set it requires an exact match.

Therefore:

* **Chain-level agents** (Manager / RegionalManager) must leave
  ``custom:property_id`` UNSET, and pass ``propertyId`` explicitly per request.
* The **Housekeeping agent** is in a group that is neither chain-level nor
  regional, so it *must* carry ``custom:property_id`` -- which pins it to exactly
  one property. It therefore gets one user per property, all sharing a single
  password, discovered from ``GET /properties`` at provision time.

Delete is deliberately a no-op: this pool is shared with 4000+ real users, and a
``cdk destroy`` of the agent stack must not reach into it and delete accounts.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")
secrets = boto3.client("secretsmanager")

HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Cognito helpers
# --------------------------------------------------------------------------- #


def _get_secret(secret_arn: str) -> dict:
    raw = secrets.get_secret_value(SecretId=secret_arn)["SecretString"]
    return json.loads(raw)


def ensure_user(
    *,
    agent: str,
    user_pool_id: str,
    username: str,
    password: str,
    group: str,
    property_id: str | None = None,
    region: str | None = None,
) -> str:
    """Create-or-update one agent user. Returns "created" or "updated".

    ``agent`` is the agent's name from the stack's own configuration, and it is what
    gets logged. ``username`` is not: it is read out of the same Secrets Manager secret
    as the password, and CodeQL (py/clear-text-logging-sensitive-data) rightly flags
    logging anything derived from a secret. A username is not a credential, but the
    habit of logging secret-sourced values is how a password ends up in CloudWatch.
    """
    attributes = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
    ]
    if property_id:
        attributes.append({"Name": "custom:property_id", "Value": property_id})
    if region:
        attributes.append({"Name": "custom:region", "Value": region})

    outcome = "created"
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=attributes,
            # SUPPRESS: never email an invite to a machine identity.
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        outcome = "updated"
        cognito.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=attributes,
        )

    # Permanent password: skips the FORCE_CHANGE_PASSWORD challenge that would
    # otherwise make ADMIN_USER_PASSWORD_AUTH return a challenge, not tokens.
    cognito.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )

    cognito.admin_add_user_to_group(
        UserPoolId=user_pool_id,
        Username=username,
        GroupName=group,
    )

    logger.info(
        "agent identity %s: agent=%s group=%s property_id=%s region=%s",
        outcome,
        agent,
        group,
        property_id or "<unset>",
        region or "<unset>",
    )
    return outcome


# --------------------------------------------------------------------------- #
# Property discovery
# --------------------------------------------------------------------------- #


def _id_token(user_pool_id: str, client_id: str, username: str, password: str) -> str:
    resp = cognito.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    result = resp.get("AuthenticationResult")
    if not result:
        raise RuntimeError(
            f"ADMIN_USER_PASSWORD_AUTH for {username} returned a challenge, not "
            f"tokens: {resp.get('ChallengeName')}"
        )
    # IdToken, not AccessToken: cognito:groups and custom:* live only here.
    return result["IdToken"]


def list_property_ids(*, pms_api_url: str, token: str) -> list[str]:
    """GET /properties on the PMS API. Chain-level callers get all of them."""
    # The URL is a CloudFormation property from the platform's own stack output, so
    # it is not attacker input; checked anyway because urllib would follow file:// and
    # this function runs with rights to create users in the platform's user pool.
    scheme = urllib.parse.urlparse(pms_api_url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"PmsApiUrl has disallowed scheme {scheme!r}")
    request = urllib.request.Request(
        f"{pms_api_url.rstrip('/')}/properties",
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - scheme validated above
            request, timeout=HTTP_TIMEOUT
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(
            f"GET /properties failed with {exc.code}: {detail}"
        ) from exc

    if not body.get("success"):
        raise RuntimeError(f"GET /properties returned an error envelope: {body}")

    properties = body.get("data", {}).get("properties", [])
    ids = [p["propertyId"] for p in properties if p.get("propertyId")]
    if not ids:
        raise RuntimeError(
            "GET /properties returned no properties. The foundation's seed data "
            "may not have been loaded."
        )
    return ids


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


def on_create_or_update(props: dict) -> dict:
    user_pool_id = props["UserPoolId"]
    admin_client_id = props["AdminAuthClientId"]
    pms_api_url = props["PmsApiUrl"]

    summary: dict[str, object] = {}

    # --- 1. Chain-level agents: property_id deliberately UNSET. -------------
    chain_agents = props["ChainAgents"]
    for agent in chain_agents:
        creds = _get_secret(agent["SecretArn"])
        ensure_user(
            agent=agent["Name"],
            user_pool_id=user_pool_id,
            username=creds["username"],
            password=creds["password"],
            group=agent["Group"],
            # RegionalManager may be region-scoped; unset means all regions.
            region=agent.get("Region") or None,
        )
    summary["chainAgents"] = len(chain_agents)

    # --- 2. Property-scoped agents: one user per property. ------------------
    property_agent = props.get("PropertyAgent")
    if property_agent:
        # Discover properties using a chain-level agent's own token, so this
        # needs no extra credential and no database access.
        discovery = next(
            a for a in chain_agents if a["Name"] == property_agent["DiscoveryAgent"]
        )
        discovery_creds = _get_secret(discovery["SecretArn"])
        token = _id_token(
            user_pool_id,
            admin_client_id,
            discovery_creds["username"],
            discovery_creds["password"],
        )
        property_ids = list_property_ids(pms_api_url=pms_api_url, token=token)

        creds = _get_secret(property_agent["SecretArn"])
        template = property_agent["UsernameTemplate"]
        for property_id in property_ids:
            ensure_user(
                agent=property_agent["Name"],
                user_pool_id=user_pool_id,
                username=template.format(property_id=property_id),
                password=creds["password"],
                group=property_agent["Group"],
                property_id=property_id,
            )
        summary["propertyAgentUsers"] = len(property_ids)
        summary["propertyIds"] = ",".join(property_ids[:5]) + (
            f",... (+{len(property_ids) - 5})" if len(property_ids) > 5 else ""
        )

    return summary


def handler(event: dict, _context) -> dict:
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId") or "hotel-ops-agent-identities"

    logger.info("RequestType=%s", request_type)

    if request_type == "Delete":
        # Intentional no-op. The pool is shared with thousands of real users;
        # tearing down this stack must never delete accounts inside it.
        logger.info("Delete is a no-op: agent Cognito users are retained.")
        return {"PhysicalResourceId": physical_id, "Data": {"retained": "true"}}

    try:
        summary = on_create_or_update(props)
    except ClientError as exc:
        # Surface the real Cognito error; the CFN failure reason is all the
        # operator gets, so it needs to be legible.
        raise RuntimeError(
            f"Cognito call failed: {exc.response['Error']['Code']} "
            f"{exc.response['Error']['Message']}"
        ) from exc

    return {
        "PhysicalResourceId": physical_id,
        "Data": {k: str(v) for k, v in summary.items()},
    }
