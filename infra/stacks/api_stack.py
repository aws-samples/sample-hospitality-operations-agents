# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ops console's backend: its own API, authorized by the foundation's pool.

Three functions behind one REST API, with a Cognito authorizer against the
*existing* user pool so hotel staff sign in with the credentials they already have.
No new user directory, and -- importantly -- no new app client: the foundation's
SPA client already permits ``USER_SRP_AUTH``, which is all a login form needs. A new
app client would have been a foundation-side resource the plan does not permit,
and Hosted UI would have required adding callback URLs to a client this project must
not touch.

Why the stage is called "api"
-----------------------------
``frontend_stack`` puts this API and the console's S3 bucket behind **one**
CloudFront distribution: ``/api/*`` routes here, everything else to the bucket. That
makes every request same-origin, so there is no CORS configuration anywhere in this
stack and no preflight on any request -- a deliberate contrast with the wildcard
CORS the foundation carries.

The trick that makes it free: an API Gateway stage named ``api`` serves paths at
``/api/runs``, so CloudFront can forward ``/api/*`` to this origin with no path
rewriting, no origin path, and no CloudFront Function.

Why chat does not stream
------------------------
The plan asked for ``POST /chat`` to stream from the Runtime. API Gateway's default
REST integration timeout is 29 seconds and the runs this system produces take 30 to
650, so every real chat message would have timed out. ``chat/index.py`` explains the
design that replaced it and why it is better rather than merely workable; the short
version is that a chat message goes onto a queue like every other invocation, and
the console watches the decision log fill in.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.foundation_config import FoundationConfig
from stacks.orchestration_stack import (
    APPROVALS_BY_STATUS_INDEX,
    DECISIONS_BY_PROPERTY_INDEX,
    OrchestrationStack,
)
from stacks.tools_stack import ASSET_EXCLUDE

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = REPO_ROOT / "infra" / "lambdas" / "console"

#: The stage name is load-bearing, not cosmetic. See the module docstring: it is
#: what lets CloudFront forward /api/* here without rewriting the path.
STAGE_NAME = "api"

#: Written by scripts/register_staff_scope.py, read by every console function.
STAFF_SCOPE_TABLE_NAME = "hotel-ops-agent-staff-scope"

#: How long a released approval stays spendable. Minutes, because the token is not
#: single-use -- this window is the containment, along with the gates' binding of the
#: token to its action, target and every argument the proposal captured.
APPROVAL_TTL = Duration.minutes(15)

#: An unreleased proposal's lifetime. It carries no authority; the TTL exists so the
#: queue does not fill with last month's untouched requests.
PENDING_TTL = Duration.days(7)


class ApiStack(Stack):
    """The console's own API Gateway, Lambdas, and Cognito authorizer."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationConfig,
        orchestration: OrchestrationStack,
        log_level: str = "INFO",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.foundation = foundation

        layer = lambda_.LayerVersion(
            self,
            "ConsoleLayer",
            code=lambda_.Code.from_asset(
                str(CONSOLE_DIR / "layer"), exclude=ASSET_EXCLUDE
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            removal_policy=RemovalPolicy.DESTROY,
            description=(
                "hotel_console: caller identity from verified Cognito claims, "
                "envelope rendering, and DynamoDB table access"
            ),
        )

        # ------------------------------------------------------------------ #
        # The three functions
        # ------------------------------------------------------------------ #
        # ------------------------------------------------------------------ #
        # Who is scoped to what -- decided here, not by the user
        # ------------------------------------------------------------------ #
        # The platform lets a signed-in user rewrite their own custom:property_id
        # and custom:region through its SPA app client, so those claims say whatever
        # the user last set. Scope comes from this table instead, written only with
        # AWS IAM credentials by scripts/register_staff_scope.py; a Cognito user has
        # no path to it. Read-only for every console function.
        self.staff_scope = dynamodb.Table(
            self,
            "StaffScopeTable",
            table_name=STAFF_SCOPE_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="sub", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Retained: losing it locks every operator out until they are
            # re-registered, and re-seeding from the pool would re-trust whatever the
            # users have written to their own attributes since.
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )

        common_env = {
            "STAFF_SCOPE_TABLE": self.staff_scope.table_name,
            "DECISIONS_TABLE": orchestration.decisions.table_name,
            "APPROVALS_TABLE": orchestration.approvals.table_name,
            "LOG_LEVEL": log_level,
        }

        self.chat_fn = self._function(
            "ChatFn",
            "hotel-ops-agent-console-chat",
            "chat",
            layer,
            {**common_env, "CHAT_QUEUE_URL": orchestration.chat_queue.queue_url},
            description=(
                "POST /chat: queues an operator's question as a run carrying their "
                "own Cognito groups"
            ),
        )
        orchestration.chat_queue.grant_send_messages(self.chat_fn)
        self.chat_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RecordQueuedRun",
                # PutItem only, and only so a run is visible the instant it is
                # enqueued rather than after the invoker's first tool call. Without
                # it the console polled GET /runs/{id} into a 404 for the first
                # seconds of every run -- 450 of them in one day, plus throttling.
                actions=["dynamodb:PutItem"],
                resources=[orchestration.decisions.table_arn],
            )
        )

        self.runs_fn = self._function(
            "RunsFn",
            "hotel-ops-agent-console-runs",
            "runs",
            layer,
            common_env,
            description=(
                "GET /runs, GET /runs/{runId}, POST /runs/{runId}/override: the "
                "decision log read back, and the human verdict on it"
            ),
        )
        self.runs_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadAndAnnotateDecisionLog",
                # Query, not Scan: every read this function makes is keyed, and
                # withholding Scan means a bug cannot turn into a full-table read of
                # every decision the system has ever made.
                #
                # UpdateItem, not PutItem: the override is stamped onto a row the
                # interceptor or invoker already wrote, and PutItem could replace one
                # wholesale. No DeleteItem anywhere -- this is the audit trail.
                actions=["dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[
                    orchestration.decisions.table_arn,
                    f"{orchestration.decisions.table_arn}/index/{DECISIONS_BY_PROPERTY_INDEX}",
                ],
            )
        )

        self.approvals_fn = self._function(
            "ApprovalsFn",
            "hotel-ops-agent-console-approvals",
            "approvals",
            layer,
            {
                **common_env,
                "CHAT_QUEUE_URL": orchestration.chat_queue.queue_url,
                "APPROVAL_TTL_SECONDS": str(int(APPROVAL_TTL.to_seconds())),
                "PENDING_TTL_SECONDS": str(int(PENDING_TTL.to_seconds())),
            },
            description=(
                "GET/POST /approvals and the approve|reject routes: the only place "
                "in the system where money movement is released"
            ),
        )
        orchestration.chat_queue.grant_send_messages(self.approvals_fn)
        self.approvals_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageApprovals",
                # Scan is granted here and nowhere else: resolving the console's
                # public proposal handle to the record needs it, and the alternative
                # was a second GSI whose only purpose was to save milliseconds on a
                # human's button press. The table holds tens of rows behind a TTL.
                actions=[
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    orchestration.approvals.table_arn,
                    f"{orchestration.approvals.table_arn}/index/{APPROVALS_BY_STATUS_INDEX}",
                ],
            )
        )

        # The only console function that talks to the hotel platform, and it only
        # reads. It needs no IAM beyond its log group and no table access at all --
        # its authority is entirely the caller's own forwarded ID token.
        self.properties_fn = self._function(
            "PropertiesFn",
            "hotel-ops-agent-console-properties",
            "properties",
            layer,
            {
                "PMS_API_URL": foundation.pms_api_url,
                "STAFF_SCOPE_TABLE": self.staff_scope.table_name,
                "LOG_LEVEL": log_level,
            },
            description=(
                "GET /properties: the picker's list, scoped by the platform from the "
                "operator's own token"
            ),
        )

        # Read-only, and GetItem only: a console function looks up its caller and
        # nothing else. No function here can write a scope.
        for fn in (self.chat_fn, self.runs_fn, self.approvals_fn, self.properties_fn):
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    sid="ReadCallerScope",
                    actions=["dynamodb:GetItem"],
                    resources=[self.staff_scope.table_arn],
                )
            )

        # ------------------------------------------------------------------ #
        # The API
        # ------------------------------------------------------------------ #
        self.api = apigateway.RestApi(
            self,
            "ConsoleApi",
            rest_api_name="hotel-ops-agent-console",
            description=(
                "Ops console backend. Cognito-authorized against the foundation's "
                "existing user pool; fronted by CloudFront at /api/*"
            ),
            deploy_options=apigateway.StageOptions(
                stage_name=STAGE_NAME,
                # Access logs and metrics on, tracing on. This API sits in front of
                # the approval queue; "who released that charge, and when" must be
                # answerable from logs and not only from the table.
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=False,  # bodies carry approval tokens
                metrics_enabled=True,
                tracing_enabled=True,
                access_log_destination=apigateway.LogGroupLogDestination(
                    logs.LogGroup(
                        self,
                        "ConsoleApiAccessLogs",
                        log_group_name="/aws/apigateway/hotel-ops-agent-console",
                        retention=logs.RetentionDays.THREE_MONTHS,
                        removal_policy=RemovalPolicy.DESTROY,
                    )
                ),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                # Modest, and deliberately set: this is a staff console with a
                # handful of users, and every POST /chat costs a model invocation.
                throttling_rate_limit=20,
                throttling_burst_limit=40,
            ),
            # No default_cors_preflight_options anywhere: same-origin behind
            # CloudFront, so there is nothing to preflight. See the module docstring.
            endpoint_configuration=apigateway.EndpointConfiguration(
                # Regional, because CloudFront is doing the edge work. An
                # edge-optimized endpoint behind CloudFront is two CDNs in series.
                types=[apigateway.EndpointType.REGIONAL]
            ),
            cloud_watch_role=True,
        )

        authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "ConsoleAuthorizer",
            authorizer_name="hotel-ops-agent-console-cognito",
            cognito_user_pools=[
                cognito.UserPool.from_user_pool_id(
                    self, "FoundationUserPool", foundation.user_pool_id
                )
            ],
            # The console sends the ID token, not the access token: only the ID token
            # carries cognito:groups and custom:property_id, and those two claims are
            # the entire authorization model. An access token here would authenticate
            # a caller the handlers could then not scope.
            identity_source=apigateway.IdentitySource.header("Authorization"),
            results_cache_ttl=Duration.minutes(5),
        )
        self.authorizer = authorizer

        def secured(resource: apigateway.IResource, method: str, fn: lambda_.Function):
            resource.add_method(
                method,
                apigateway.LambdaIntegration(fn, proxy=True),
                authorizer=authorizer,
                authorization_type=apigateway.AuthorizationType.COGNITO,
            )

        # POST /chat
        secured(self.api.root.add_resource("chat"), "POST", self.chat_fn)

        # GET /properties
        secured(self.api.root.add_resource("properties"), "GET", self.properties_fn)

        # /runs, /runs/{runId}, /runs/{runId}/override
        runs = self.api.root.add_resource("runs")
        secured(runs, "GET", self.runs_fn)
        run = runs.add_resource("{runId}")
        secured(run, "GET", self.runs_fn)
        secured(run.add_resource("override"), "POST", self.runs_fn)

        # /approvals, /approvals/{id}/approve, /approvals/{id}/reject
        approvals = self.api.root.add_resource("approvals")
        secured(approvals, "GET", self.approvals_fn)
        secured(approvals, "POST", self.approvals_fn)
        approval = approvals.add_resource("{id}")
        secured(approval.add_resource("approve"), "POST", self.approvals_fn)
        secured(approval.add_resource("reject"), "POST", self.approvals_fn)

        # ------------------------------------------------------------------ #
        # Outputs
        # ------------------------------------------------------------------ #
        #: The API Gateway host, without scheme or path. frontend_stack needs exactly
        #: this to build a CloudFront origin, and a full URL would have to be
        #: re-parsed there.
        self.api_domain = f"{self.api.rest_api_id}.execute-api.{self.region}.amazonaws.com"

        CfnOutput(
            self,
            "ConsoleApiUrl",
            value=self.api.url,
            description=(
                "Direct API URL. The console does not use this -- it calls /api/* on "
                "the CloudFront distribution -- but curl and the verification script do."
            ),
        )
        CfnOutput(self, "ConsoleApiId", value=self.api.rest_api_id)
        CfnOutput(
            self,
            "StaffScopeTableName",
            value=self.staff_scope.table_name,
            description="Register console users here with scripts/register_staff_scope.py",
        )
        CfnOutput(
            self,
            "ConsoleApiDomain",
            value=self.api_domain,
            description="Origin domain for the CloudFront /api/* behaviour",
        )
        CfnOutput(
            self,
            "ConsoleUserPoolId",
            value=foundation.user_pool_id,
            description="The foundation's pool. Staff sign in with existing accounts.",
        )
        CfnOutput(
            self,
            "ConsoleUserPoolClientId",
            value=foundation.user_pool_client_id,
            description=(
                "The foundation's existing SPA client, which already allows "
                "USER_SRP_AUTH. No new app client was created: that would be a "
                "foundation-side resource this project must not add."
            ),
        )

    # ---------------------------------------------------------------- helpers

    def _function(
        self,
        construct_id: str,
        function_name: str,
        source: str,
        layer: lambda_.LayerVersion,
        environment: dict,
        *,
        description: str,
    ) -> lambda_.Function:
        return lambda_.Function(
            self,
            construct_id,
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                str(CONSOLE_DIR / source), exclude=ASSET_EXCLUDE
            ),
            layers=[layer],
            environment=environment,
            # Every route is a keyed DynamoDB read or a queue send. Ten seconds is
            # generous; anything slower is a fault, and failing fast keeps API
            # Gateway from holding the operator's browser.
            timeout=Duration.seconds(10),
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                f"{construct_id}Logs",
                log_group_name=f"/aws/lambda/{function_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description=description,
        )
