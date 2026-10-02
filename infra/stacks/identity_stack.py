# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Agent Cognito identities and their credentials.

Additive-only. This stack creates *users* inside the foundation's existing user
pool and nothing else: no pool settings, no groups, no app clients, no schema
changes. The pool itself remains owned by ``anycompany-booking``.

Why users at all
----------------
The foundation authorizes on ``cognito:groups`` and ``custom:property_id``
(``src/layers/common/utils/tenant.py``). Those claims exist only in a Cognito
user ID token, so the tool Lambdas must sign in as real users -- an OAuth
client-credentials token would be rejected by ``require_groups`` on every write.

Why two identity shapes
-----------------------
``verify_property_access`` grants access to a caller with ``custom:property_id``
unset only when the caller is in ``CHAIN_LEVEL_GROUPS`` (Admin, Manager,
RevenueManager) or ``REGIONAL_GROUPS`` (RegionalManager). ``Housekeeping`` is in
neither, so the housekeeping agent *must* carry ``custom:property_id`` -- which
pins one user to exactly one property. Hence:

* 4 chain-level users with ``custom:property_id`` unset, passing ``propertyId``
  explicitly per request.
* 1 housekeeping user per property, discovered at deploy time from
  ``GET /properties``, all sharing a single password.

See :mod:`infra.lambdas.agent_identity_provisioner.index` for the mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks.foundation_config import FoundationConfig

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambdas"


@dataclass(frozen=True)
class AgentIdentity:
    """One agent's Cognito identity and the secret holding its credentials."""

    #: Short name; also the secret path segment and the local part of the email.
    name: str
    #: Cognito group the user is added to. Never ``Admin`` (PATTERN_EXTENSION_GUIDE.md §3.1).
    group: str
    secret: secretsmanager.Secret
    username_template: str

    @property
    def is_property_scoped(self) -> bool:
        return "{property_id}" in self.username_template


#: Chain-level agents. ``custom:property_id`` stays UNSET so that
#: ``verify_property_access`` takes its chain-level/regional branch and permits
#: cross-property work; each call passes ``propertyId`` explicitly instead.
CHAIN_AGENTS: tuple[tuple[str, str], ...] = (
    ("arrivals", "Manager"),
    ("billing", "Manager"),
    ("nightaudit", "Manager"),
    ("regional", "RegionalManager"),
)

#: The agent whose group is neither chain-level nor regional, and which therefore
#: needs one user per property.
PROPERTY_AGENT: tuple[str, str] = ("housekeeping", "Housekeeping")

#: Chain-level agent used to enumerate properties at provision time. Must be one
#: of CHAIN_AGENTS and must be able to read ``GET /properties``.
DISCOVERY_AGENT = "arrivals"


class IdentityStack(Stack):
    """Creates the agent credentials, then the Cognito users that use them."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationConfig,
        email_domain: str = "anycompany.internal",
        salt: str = "1",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.foundation = foundation
        self.email_domain = email_domain

        # --- Credentials ---------------------------------------------------- #
        self.agents: dict[str, AgentIdentity] = {}

        for name, group in CHAIN_AGENTS:
            username = f"agent-{name}@{email_domain}"
            self.agents[name] = AgentIdentity(
                name=name,
                group=group,
                secret=self._agent_secret(name, username),
                username_template=username,
            )

        hk_name, hk_group = PROPERTY_AGENT
        # The per-property users share one password, so the stored username is a
        # template rather than a resolved address. The provisioner formats it.
        hk_template = f"agent-{hk_name}+{{property_id}}@{email_domain}"
        self.agents[hk_name] = AgentIdentity(
            name=hk_name,
            group=hk_group,
            secret=self._agent_secret(hk_name, hk_template),
            username_template=hk_template,
        )
        self.property_agent = self.agents[hk_name]

        # --- Provisioner ---------------------------------------------------- #
        provisioner = self._provisioner_function()

        provisioner.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageAgentUsersOnly",
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminUpdateUserAttributes",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminAddUserToGroup",
                    # Needed to mint the token used for property discovery.
                    "cognito-idp:AdminInitiateAuth",
                ],
                resources=[foundation.user_pool_arn],
            )
        )
        # Deliberately absent: AdminDeleteUser, AdminRemoveUserFromGroup,
        # AdminDisableUser, and every Update*Pool / *Group action. This role
        # physically cannot damage the 4000+ real staff users in the shared pool.

        for agent in self.agents.values():
            agent.secret.grant_read(provisioner)

        provider = cr.Provider(
            self,
            "AgentIdentityProvider",
            on_event_handler=provisioner,
            log_group=self._log_group("AgentIdentityProviderLogs"),
        )

        self.identities = CustomResource(
            self,
            "AgentIdentities",
            service_token=provider.service_token,
            resource_type="Custom::HotelOpsAgentIdentities",
            properties={
                "UserPoolId": foundation.user_pool_id,
                "AdminAuthClientId": foundation.admin_auth_client_id,
                "PmsApiUrl": foundation.pms_api_url,
                "ChainAgents": [
                    {
                        "Name": agent.name,
                        "Group": agent.group,
                        "SecretArn": agent.secret.secret_arn,
                    }
                    for agent in self.agents.values()
                    if not agent.is_property_scoped
                ],
                "PropertyAgent": {
                    "Name": self.property_agent.name,
                    "Group": self.property_agent.group,
                    "SecretArn": self.property_agent.secret.secret_arn,
                    "UsernameTemplate": self.property_agent.username_template,
                    "DiscoveryAgent": DISCOVERY_AGENT,
                },
                # Bump `identitySalt` in cdk.json to force a re-run after new
                # properties are added to the foundation, so they get a
                # housekeeping user without any other change to this stack.
                "Salt": salt,
            },
        )

        # --- Outputs -------------------------------------------------------- #
        for agent in self.agents.values():
            CfnOutput(
                self,
                f"{agent.name.capitalize()}SecretArn",
                value=agent.secret.secret_arn,
                description=(
                    f"Credentials for the {agent.name} agent "
                    f"(Cognito group {agent.group})"
                ),
            )

        CfnOutput(
            self,
            "HousekeepingUsernameTemplate",
            value=self.property_agent.username_template,
            description=(
                "Per-property housekeeping username; format with the propertyId"
            ),
        )
        CfnOutput(
            self,
            "HousekeepingUserCount",
            value=self.identities.get_att_string("propertyAgentUsers"),
            description="Housekeeping users provisioned, one per active property",
        )

    # ---------------------------------------------------------------- helpers

    def _log_group(self, construct_id: str) -> logs.LogGroup:
        """An explicit log group, rather than the deprecated ``log_retention``.

        ``log_retention`` would provision an extra singleton Lambda holding
        account-wide ``logs:PutRetentionPolicy``; an explicit group needs no such
        role and is deleted with the stack.
        """
        return logs.LogGroup(
            self,
            construct_id,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _agent_secret(self, name: str, username: str) -> secretsmanager.Secret:
        """A ``{"username", "password"}`` secret with a generated password.

        ``exclude_punctuation`` + ``require_each_included_type`` produce a
        password that satisfies the foundation pool's policy (min 8 chars, upper,
        lower, digits; symbols not required) while staying safe to embed in the
        JSON auth payload.
        """
        return secretsmanager.Secret(
            self,
            f"{name.capitalize()}AgentCredentials",
            secret_name=f"hotel-ops-agent/{name}",
            description=f"Cognito credentials for the {name} hotel-ops agent",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=f'{{"username":"{username}"}}',
                generate_string_key="password",
                exclude_punctuation=True,
                require_each_included_type=True,
                password_length=24,
            ),
            # RETAIN: the per-property housekeeping users all authenticate with
            # this one password, so losing it silently breaks 50 identities.
            # Caveat: a `cdk destroy` followed by a redeploy will collide on the
            # secret name. To recycle it deliberately:
            #   aws secretsmanager delete-secret --secret-id hotel-ops-agent/<name> \
            #     --force-delete-without-recovery
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _provisioner_function(self) -> lambda_.Function:
        return lambda_.Function(
            self,
            "AgentIdentityProvisioner",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                str(LAMBDA_DIR / "agent_identity_provisioner")
            ),
            # ~4 Cognito calls per user x (4 chain + N property) users, plus one
            # HTTPS round trip to the PMS API. 50 properties fits comfortably.
            timeout=Duration.minutes(10),
            memory_size=512,
            log_group=self._log_group("AgentIdentityProvisionerLogs"),
            environment={"LOG_LEVEL": "INFO"},
            description=(
                "CloudFormation custom resource: creates the hotel-ops agent "
                "users in the foundation Cognito pool. Delete is a no-op."
            ),
        )
