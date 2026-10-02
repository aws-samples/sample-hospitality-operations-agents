# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The tool plane: five Gateway target Lambdas, one per agent domain.

Each function is the *only* way its sub-agent reaches the foundation, and each
signs in as a different Cognito user. That is what makes the permission model
real rather than advisory: the housekeeping function has no path to a billing
endpoint because its token is in the ``Housekeeping`` group, which
``require_groups`` rejects on every billing write. The separation is enforced by
the foundation's own authorizer, not by prompt wording.

Why one function per domain and not one per tool
-----------------------------------------------
A Cognito ``admin_initiate_auth`` round trip costs real latency, and the token is
cached in module scope for the life of a warm container. Grouping a domain's
tools into one function means an agent working through a task list pays that cost
once. Splitting per tool would multiply cold starts by 28 for no isolation gain --
the isolation boundary that matters is the *identity*, and identity is per domain.

What is deliberately not granted
--------------------------------
No ``execute-api:Invoke``. The foundation's APIs use a Cognito authorizer, not
IAM auth, so an IAM grant would be cargo cult -- the ID token is the credential.
No wildcards on Secrets Manager: each function reads exactly one secret, so a bug
in the arrivals function cannot surface the housekeeping password.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.foundation_config import FoundationConfig
from stacks.identity_stack import IdentityStack

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCHEMAS_DIR = REPO_ROOT / "schemas"

#: Excluded from every code asset. Running ``pytest`` imports the handlers and the
#: shared layer, which leaves ``__pycache__`` beside the sources; without this the
#: asset hash changes after every test run, and a ``cdk deploy`` replaces the layer
#: and updates all five functions to ship byte-identical logic. Worse, the churn
#: hides a real code change in the noise.
ASSET_EXCLUDE = ["__pycache__", "*.pyc", "*.pyo"]

#: Gateway target name -> the agent identity in :mod:`stacks.identity_stack` it
#: signs in as. The target name is also the directory under ``tools/`` and the
#: stem of the schema file under ``schemas/``, so the three cannot drift apart
#: silently -- :func:`_verify_target` checks all three at synth time.
TARGETS: tuple[str, ...] = (
    "arrivals",
    "housekeeping",
    "billing",
    "nightaudit",
    "regional",
)

#: Targets that hold at least one Tier-2 tool and therefore need to read the
#: approvals table. Only billing moves money.
APPROVAL_GATED: frozenset[str] = frozenset({"billing"})

#: Name of the approvals table, created later by ``orchestration_stack``.
#: Referenced by name rather than by construct on purpose: a CFN reference would
#: make this stack depend on a stack that does not exist yet, and the handler
#: already fails closed (``APPROVAL_UNVERIFIABLE``) when the table is missing.
#: So until Phase 3 lands, every Tier-2 write is refused -- the correct default.
APPROVALS_TABLE_NAME = "hotel-ops-agent-approvals"


class ToolsStack(Stack):
    """The five Lambda functions AgentCore Gateway will expose as MCP tools."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationConfig,
        identity: IdentityStack,
        api_pacing_ms: int = 50,
        log_level: str = "INFO",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.foundation = foundation
        self.identity = identity

        layer = lambda_.LayerVersion(
            self,
            "HotelOpsToolLayer",
            code=lambda_.Code.from_asset(str(TOOLS_DIR / "layer"), exclude=ASSET_EXCLUDE),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            removal_policy=RemovalPolicy.DESTROY,
            description=(
                "hotel_ops shared library: FoundationClient (Cognito ID token + "
                "paced HTTP) and ToolRouter (Gateway tool-name dispatch)"
            ),
        )

        approvals_table_arn = self.format_arn(
            service="dynamodb", resource="table", resource_name=APPROVALS_TABLE_NAME
        )

        self.functions: dict[str, lambda_.Function] = {}
        self.schema_paths: dict[str, str] = {}

        for target in TARGETS:
            schema_path = SCHEMAS_DIR / f"{target}.json"
            source_dir = TOOLS_DIR / target
            tool_names = _verify_target(target, source_dir, schema_path)
            self.schema_paths[target] = str(schema_path)

            agent = identity.agents[target]

            environment = {
                "CRS_API_URL": foundation.crs_api_url,
                "PMS_API_URL": foundation.pms_api_url,
                "COGNITO_USER_POOL_ID": foundation.user_pool_id,
                "ADMIN_AUTH_CLIENT_ID": foundation.admin_auth_client_id,
                "AGENT_CREDS_SECRET_ARN": agent.secret.secret_arn,
                # The housekeeping template carries "{property_id}"; the client
                # keys its token cache on the resolved username, so one warm
                # container can hold a token per property.
                "AGENT_USERNAME_TEMPLATE": agent.username_template,
                "API_PACING_MS": str(api_pacing_ms),
                "LOG_LEVEL": log_level,
            }
            # DEFAULT_PAGE_LIMIT is left unset on purpose: each handler documents
            # its own sensible default, and overriding it here would create two
            # places that disagree about what "a page" means.
            if target in APPROVAL_GATED:
                environment["APPROVALS_TABLE"] = APPROVALS_TABLE_NAME

            function = lambda_.Function(
                self,
                f"Tool{target.capitalize()}Fn",
                function_name=f"hotel-ops-agent-tool-{target}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler="handler.handler",
                code=lambda_.Code.from_asset(str(source_dir), exclude=ASSET_EXCLUDE),
                layers=[layer],
                environment=environment,
                # Worst case is two sequential foundation calls behind one
                # Cognito auth; 60s leaves room for a cold start plus a retry
                # after a forced token refresh, without letting the Gateway hang.
                timeout=Duration.seconds(60),
                memory_size=512,
                # X-Ray, so a trace runs orchestrator -> sub-agent -> Gateway ->
                # this function -> foundation API in one view.
                tracing=lambda_.Tracing.ACTIVE,
                log_group=self._log_group(f"Tool{target.capitalize()}Logs", target),
                description=(
                    f"AgentCore Gateway target '{target}': "
                    f"{len(tool_names)} tools, authenticating as Cognito group "
                    f"{agent.group}"
                ),
            )

            function.add_to_role_policy(
                iam.PolicyStatement(
                    sid="AuthenticateAsAgentUser",
                    actions=["cognito-idp:AdminInitiateAuth"],
                    resources=[foundation.user_pool_arn],
                )
            )
            # Exactly one secret, no wildcard: this function cannot read another
            # agent's password even if its own code is compromised.
            agent.secret.grant_read(function)

            if target in APPROVAL_GATED:
                function.add_to_role_policy(
                    iam.PolicyStatement(
                        sid="ReadApprovalTokens",
                        # Read only. An agent must never be able to mint, alter,
                        # or consume its own approval -- that is the human's act.
                        actions=["dynamodb:GetItem"],
                        resources=[approvals_table_arn],
                    )
                )

            self.functions[target] = function

            CfnOutput(
                self,
                f"Tool{target.capitalize()}FnArn",
                value=function.function_arn,
                description=f"Gateway Lambda target for the {target} domain",
            )

    # ---------------------------------------------------------------- helpers

    def _log_group(self, construct_id: str, target: str) -> logs.LogGroup:
        """Explicit log group per function, rather than ``log_retention``.

        ``log_retention`` provisions a singleton Lambda holding account-wide
        ``logs:PutRetentionPolicy``; naming the group ourselves needs no such
        role, and the group is removed with the stack.
        """
        return logs.LogGroup(
            self,
            construct_id,
            log_group_name=f"/aws/lambda/hotel-ops-agent-tool-{target}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )


# -------------------------------------------------------------------------- #
# Synth-time consistency check
# -------------------------------------------------------------------------- #

_ROUTER_TARGET = re.compile(r'ToolRouter\(\s*"([^"]+)"\s*\)')
_TOOL_DECORATOR = re.compile(r'@router\.tool\(\s*"([^"]+)"\s*\)')

#: ``SchemaDefinition`` in the AgentCore API has exactly these fields. Notably no
#: ``enum``, no ``default``, no ``format`` -- allowed values have to be described
#: in prose, and a stray key would be rejected at deploy time, not at synth.
_SCHEMA_DEF_KEYS = frozenset({"type", "description", "items", "properties", "required"})
_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "number", "object", "string"}
)


def _verify_target(target: str, source_dir: Path, schema_path: Path) -> list[str]:
    """Assert the schema the Gateway advertises matches what the Lambda routes.

    Worth failing the synth over. The Gateway learns a target's tools from the
    schema file, but dispatch happens from the ``@router.tool`` registry in the
    handler; if the two disagree, the model is handed a tool that returns
    ``UNKNOWN_TOOL`` at runtime and has no way to understand why. This is the
    single most likely first-run bug in the whole tool plane, and it is trivially
    detectable here.
    """
    handler_path = source_dir / "handler.py"
    if not handler_path.is_file():
        raise FileNotFoundError(f"Gateway target {target!r}: missing {handler_path}")
    if not schema_path.is_file():
        raise FileNotFoundError(f"Gateway target {target!r}: missing {schema_path}")

    source = handler_path.read_text(encoding="utf-8")
    router = _ROUTER_TARGET.search(source)
    if not router:
        raise ValueError(f"{handler_path}: no ToolRouter(\"...\") found")
    if router.group(1) != target:
        raise ValueError(
            f"{handler_path}: ToolRouter target is {router.group(1)!r} but the "
            f"directory is {target!r}. The Gateway prefixes tool names with the "
            "target name, so these must agree or prefix stripping breaks."
        )
    registered = _TOOL_DECORATOR.findall(source)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, list):
        raise ValueError(
            f"{schema_path}: must be a JSON array of ToolDefinition objects "
            "(this file is uploaded verbatim and read by AgentCore)"
        )

    declared: list[str] = []
    for index, tool in enumerate(schema):
        for key in ("name", "description", "inputSchema"):
            if key not in tool:
                raise ValueError(f"{schema_path}[{index}]: missing {key!r}")
        extra = set(tool) - {"name", "description", "inputSchema", "outputSchema"}
        if extra:
            raise ValueError(f"{schema_path}[{index}]: unknown key(s) {sorted(extra)}")
        declared.append(tool["name"])
        _verify_schema_definition(
            tool["inputSchema"], f"{schema_path}:{tool['name']}.inputSchema"
        )

    if sorted(declared) != sorted(registered):
        only_schema = sorted(set(declared) - set(registered))
        only_handler = sorted(set(registered) - set(declared))
        raise ValueError(
            f"Gateway target {target!r}: schema and handler disagree.\n"
            f"  advertised but not routed: {only_schema or 'none'}\n"
            f"  routed but not advertised: {only_handler or 'none'}"
        )
    if len(set(declared)) != len(declared):
        raise ValueError(f"{schema_path}: duplicate tool names")

    return declared


def _verify_schema_definition(node: object, where: str) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"{where}: must be an object")
    extra = set(node) - _SCHEMA_DEF_KEYS
    if extra:
        raise ValueError(
            f"{where}: unsupported key(s) {sorted(extra)}. AgentCore's "
            f"SchemaDefinition supports only {sorted(_SCHEMA_DEF_KEYS)}"
        )
    node_type = node.get("type")
    if node_type not in _SCHEMA_TYPES:
        raise ValueError(f"{where}: type must be one of {sorted(_SCHEMA_TYPES)}")

    properties = node.get("properties") or {}
    for name, child in properties.items():
        _verify_schema_definition(child, f"{where}.properties.{name}")
    for required in node.get("required") or []:
        if required not in properties:
            raise ValueError(
                f"{where}: required {required!r} is not among its properties"
            )
    if node_type == "array" and "items" in node:
        _verify_schema_definition(node["items"], f"{where}.items")
