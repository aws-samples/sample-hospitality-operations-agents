# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reasoning layer: one Gateway, one Memory, one Runtime, six agents.

This stack is the whole of AgentCore that this project uses. Reading it top to
bottom is reading the architecture:

    two interceptor Lambdas   the approval gate and the decision log
    Gateway (IAM inbound)     the single MCP tool plane
      +-- five Lambda targets  arrivals, housekeeping, billing, nightaudit, regional
    Memory                    durable facts + per-shift summaries
    CodeInterpreterCustom     sandboxed math for A5
    Runtime                   orchestrator + A1-A5 in one process
      +-- "production"         the endpoint the ops console and schedules target

Three choices here are load-bearing enough to state up front.

**One Runtime, not six.** The sub-agents are Strands agents-as-tools, so
delegation is an in-process function call. Six runtimes would buy nothing --
the orchestrator would still own routing -- and would cost six cold starts and
five network hops per fan-out.

**Inbound auth is IAM SigV4, not Cognito JWT.** This is a deliberate deviation
from the plan, forced by the service: an AgentCore Runtime supports exactly one
inbound auth method per version, and JWT bearer is not signed by the AWS SDKs.
Choosing Cognito would break ``agentcore invoke``, break the Phase-3 invoker
Lambda that every schedule and every EventBridge rule goes through, and force
the Phase-4 BFF onto raw HTTPS. The human's authority does not need a JWT to
reach the agents: it arrives in the invocation payload as ``callerGroups``,
which ``agents/run_context.py`` reads and ``subagents/base.py`` puts in the
sub-agent's context. The Cognito check still happens -- at the ops console's own
API Gateway authorizer, in front of this Runtime, in Phase 4.

**The ``production`` endpoint tracks the deployed version.** The plan pinned it
to ``version="1"``. Pinning is right when there is a promotion process to
un-pin it; with none, the second ``cdk deploy`` would leave ``production``
serving the first deploy's code while the stack reported success, and the only
symptom would be an agent that ignores your edits. ``DEFAULT`` and
``production`` therefore both follow this stack; the endpoint split remains
useful because Phase 4's console targets a name it can keep while a future
promotion process pins it.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.foundation_config import FoundationConfig
from stacks.tools_stack import APPROVALS_TABLE_NAME, TARGETS, ToolsStack

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / "build" / "agents"
LAMBDAS_DIR = REPO_ROOT / "infra" / "lambdas"

#: Runtime, Memory and Code Interpreter names accept only ``[A-Za-z0-9_]``; the
#: Gateway also accepts hyphens. Hence the inconsistent spelling below -- it is
#: the service's constraint, not a slip.
RUNTIME_NAME = "hotel_ops_agent"
MEMORY_NAME = "hotel_ops"
CODE_INTERPRETER_NAME = "hotel_ops_analytics"
GATEWAY_NAME = "hotel-ops-agent-gateway"

#: Named endpoint the ops console and the Phase-3 schedules invoke. ``DEFAULT``
#: exists implicitly and is what smoke tests use.
PRODUCTION_ENDPOINT = "production"

#: Created by ``orchestration_stack`` in Phase 3, referenced by name here for the
#: same reason ``tools_stack`` references the approvals table by name: a CFN
#: reference would make this stack depend on one that does not exist yet. Both
#: interceptors degrade correctly until it does -- the decision log logs a warning
#: and writes nothing, and the approval gate fails *closed*.
DECISIONS_TABLE_NAME = "hotel-ops-agent-decisions"

#: Long-term memory namespaces, published to the Runtime verbatim so the template
#: written to and the template retrieved from cannot drift (``agents/memory.py``
#: reads them from the environment rather than restating them).
#:
#: Only ``{actorId}`` and ``{sessionId}`` appear. The service also offers
#: ``{memoryStrategyId}`` -- and its own defaults use it -- but the Strands
#: session manager substitutes only those two, so a strategy id in the template
#: would be written literally and retrieval would match nothing. That is why
#: these are authored explicitly instead of using ``using_built_in_semantic()``.
#:
#: ``actorId`` is the agent (``arrivals``, ``night_audit``, ...) and ``sessionId``
#: is ``{property}:{operating_date}``, so facts partition per agent and summaries
#: partition per agent per shift.
FACTS_NAMESPACE = "/hotel-ops/facts/{actorId}"
SHIFT_NAMESPACE = "/hotel-ops/shift/{actorId}/{sessionId}"

#: Cross-region inference profiles route to the same foundation model in several
#: regions, and ``bedrock:InvokeModel`` is authorized against *both* the profile
#: ARN and the underlying model ARN in whichever region served the request. These
#: three are what ``GetInferenceProfile`` reports for
#: ``us.anthropic.claude-sonnet-5``; granting only ``us-east-1`` would produce an
#: intermittent AccessDenied that looks like a service fault.
INFERENCE_PROFILE_REGIONS: tuple[str, ...] = ("us-east-1", "us-east-2", "us-west-2")

_PROFILE_PREFIX = re.compile(r"^(us|eu|apac|jp|au|global)\.")

#: Files whose absence would produce a Runtime that starts, fails ``/ping``, and
#: reports CREATE_FAILED ten minutes later with nothing useful in the log. Checked
#: at synth because ``build/`` is not version-controlled: a stale or missing
#: build directory is exactly as deployable as a correct one.
REQUIRED_BUILD_ARTIFACTS: tuple[str, ...] = (
    "main.py",
    "orchestrator.py",
    "config.py",
    "run_context.py",
    "gateway.py",
    "sigv4.py",
    "memory.py",
    "model.py",
    "prompt_loader.py",
    "code_execution.py",
    "subagents/base.py",
    "subagents/arrivals.py",
    "subagents/housekeeping.py",
    "subagents/billing.py",
    "subagents/night_audit.py",
    "subagents/regional.py",
    "prompts/orchestrator.md",
    "prompts/arrivals.md",
    "prompts/housekeeping.md",
    "prompts/billing.md",
    "prompts/night_audit.md",
    "prompts/regional.md",
    # Dependencies must be vendored flat, because the unpacked zip *is* the
    # sys.path root. `strands` under a subdirectory would only import as
    # `subdir.strands`.
    "strands/__init__.py",
    "bedrock_agentcore/__init__.py",
    "boto3/__init__.py",
    "httpx/__init__.py",
)

#: ``entryPoint`` is an argv, so argv[0] is resolved from ``bin/`` and exec'd.
RUNTIME_ENTRYPOINT: tuple[str, ...] = ("opentelemetry-instrument", "main.py")

#: What MCP clients see when they connect. Worth writing carefully: with semantic
#: search on, this text plus the tool descriptions are how the Gateway decides
#: which of 28 tools to surface for a given turn.
GATEWAY_INSTRUCTIONS = (
    "Hotel operations tool plane for the AnyCompany hospitality platform. "
    "Tools are named <domain>__<action> across five domains: 'arrivals' "
    "(arriving stays, guest loyalty profiles, room inventory, room assignment, "
    "check-in), 'housekeeping' (cleaning tasks, room status board, task "
    "assignment, completion, inspection), 'billing' (folios, charges, voids, "
    "loyalty balances and adjustments), 'nightaudit' (daily and audit reports, "
    "metric comparison, open stays and folios), and 'regional' (property list, "
    "occupancy, multi-day metric ranges). Reads are safe to call freely. Writes "
    "require the operating property's id, and the three billing writes "
    "(post_charge, void_folio, adjust_loyalty) additionally require an "
    "approval_token issued by a human in the ops console -- they are refused "
    "without one."
)


class AgentCoreStack(Stack):
    """Gateway, Memory, Code Interpreter, and the Runtime hosting all six agents."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationConfig,
        tools: ToolsStack,
        model_id: str = "us.anthropic.claude-sonnet-5",
        property_scope: str = "",
        log_level: str = "INFO",
        debug_gateway_exceptions: bool = True,
        memory_expiration_days: int = 90,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Before any construct: a build directory that is absent or stale must
        # fail the synth, not deploy an empty Runtime.
        _verify_agent_build()

        self.foundation = foundation

        # ------------------------------------------------------------------ #
        # Interceptors -- the guardrail and the audit trail
        # ------------------------------------------------------------------ #
        # These are created before the Gateway because the Gateway takes them as
        # a prop. Both run on every eligible MCP exchange, so both are small,
        # boto3-only functions with short timeouts.

        approval_fn = self._interceptor_function(
            "ApprovalInterceptorFn",
            source="approval_interceptor",
            function_name="hotel-ops-agent-approval-interceptor",
            environment={"APPROVALS_TABLE": APPROVALS_TABLE_NAME, "LOG_LEVEL": log_level},
            # One ConsistentRead GetItem. Generous enough for a cold start plus
            # boto3's default retries, short enough that a wedged DynamoDB call
            # fails the tool rather than stalling the agent's turn.
            timeout=Duration.seconds(15),
            description=(
                "AgentCore Gateway REQUEST interceptor: refuses the three Tier-2 "
                "billing writes unless the call carries a human-issued approval_token"
            ),
        )
        approval_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadApprovalTokens",
                # Read only, and deliberately not GetItem+UpdateItem: consuming a
                # token is the ops console's act. The Gateway may retry an
                # interceptor invocation, and a gate that burned the token on a
                # retry would refuse a write the human did approve.
                actions=["dynamodb:GetItem"],
                resources=[
                    self.format_arn(
                        service="dynamodb",
                        resource="table",
                        resource_name=APPROVALS_TABLE_NAME,
                    )
                ],
            )
        )

        decision_log_fn = self._interceptor_function(
            "DecisionLogInterceptorFn",
            source="decision_log_interceptor",
            function_name="hotel-ops-agent-decision-log-interceptor",
            environment={"DECISIONS_TABLE": DECISIONS_TABLE_NAME, "LOG_LEVEL": log_level},
            timeout=Duration.seconds(10),
            description=(
                "AgentCore Gateway RESPONSE interceptor: writes one decision-log "
                "row per tool result and passes the response through unchanged"
            ),
        )
        decision_log_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="WriteDecisionLog",
                # PutItem only. An observer that could read the log could read
                # every other agent's decisions, and one that could delete rows
                # could erase the evidence the Phase-5 evaluators score against.
                actions=["dynamodb:PutItem"],
                resources=[
                    self.format_arn(
                        service="dynamodb",
                        resource="table",
                        resource_name=DECISIONS_TABLE_NAME,
                    )
                ],
            )
        )

        # ------------------------------------------------------------------ #
        # Gateway -- the single tool plane
        # ------------------------------------------------------------------ #
        self.gateway = agentcore.Gateway(
            self,
            "HotelOpsGateway",
            gateway_name=GATEWAY_NAME,
            # SigV4 from the Runtime's execution role. The alternative -- and the
            # construct's default when no authorizer is given -- is an
            # auto-created Cognito M2M pool, which would mean a second identity
            # system and a client secret to rotate for no gain: both sides of
            # this call are AWS principals in one account.
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
            protocol_configuration=agentcore.McpProtocolConfiguration(
                instructions=GATEWAY_INSTRUCTIONS,
                # 28 tools across five domains. Without semantic retrieval every
                # sub-agent turn would carry the full list; `agents/gateway.py`
                # additionally filters client-side to the target's own prefix.
                search_type=agentcore.McpGatewaySearchType.SEMANTIC,
                # supported_versions left to the service. Both interceptors read
                # single and batched JSON-RPC bodies, so protocol revision is not
                # something this stack needs an opinion about.
            ),
            # DEBUG surfaces the target's own error text to the client instead of
            # a generic gateway message, which is how a sub-agent gets to reason
            # about a real 409 or ForbiddenError. Dev only -- it can echo
            # foundation detail to the caller.
            exception_level=(
                agentcore.GatewayExceptionLevel.DEBUG if debug_gateway_exceptions else None
            ),
            interceptor_configurations=[
                # At most one REQUEST and one RESPONSE per gateway; the construct
                # rejects a second of either at synth.
                # Both sides get the X-Hotel-Ops-* headers `agents/gateway.py`
                # attaches. The request side enforces them -- the run's property,
                # the operator's groups, and the released approval -- and the
                # response side records them in the decision log. The request side
                # once went without, on the grounds that the approval gate needed
                # none of it and the header set also carries the caller's SigV4
                # Authorization; but the only thing carrying the run's scope to the
                # tools was then prompt text. Neither function logs headers.
                agentcore.LambdaInterceptor.for_request(
                    approval_fn, pass_request_headers=True
                ),
                agentcore.LambdaInterceptor.for_response(
                    decision_log_fn, pass_request_headers=True
                ),
            ],
            description=(
                "Hotel Operations Agent: the single MCP tool plane fronting all "
                "five domain Lambdas, with the Tier-2 approval gate as its "
                "request interceptor"
            ),
        )

        # ------------------------------------------------------------------ #
        # Targets -- one per agent domain
        # ------------------------------------------------------------------ #
        # gateway_target_name is the tool-name prefix the Gateway advertises
        # (`arrivals___assign_room` -- three underscores), and `agents/gateway.py`
        # filters a sub-agent's tool list on exactly that prefix. It must equal the
        # ToolsStack key.
        self.targets: dict[str, agentcore.GatewayTarget] = {}
        for target in TARGETS:
            self.targets[target] = self.gateway.add_lambda_target(
                f"Target{target.capitalize()}",
                gateway_target_name=target,
                lambda_function=tools.functions[target],
                # The schema file is checked into `schemas/` and uploaded
                # verbatim; `tools_stack._verify_target` already asserted at
                # synth that it matches the handler's dispatch table.
                tool_schema=agentcore.ToolSchema.from_local_asset(
                    tools.schema_paths[target]
                ),
                description=f"Foundation API tools for the {target} domain",
            )

        # ------------------------------------------------------------------ #
        # Memory
        # ------------------------------------------------------------------ #
        self.memory = agentcore.Memory(
            self,
            "HotelOpsMemory",
            memory_name=MEMORY_NAME,
            expiration_duration=Duration.days(memory_expiration_days),
            memory_strategies=[
                agentcore.MemoryStrategy.using_semantic(
                    strategy_name="operating_facts",
                    namespaces=[FACTS_NAMESPACE],
                    description=(
                        "Durable operating facts a shift should not have to "
                        "rediscover: which wings turn over slowly, which rooms "
                        "connect, which floors the housekeeping team batches."
                    ),
                ),
                agentcore.MemoryStrategy.using_summarization(
                    strategy_name="shift_summary",
                    namespaces=[SHIFT_NAMESPACE],
                    description=(
                        "Per-shift continuity, so a nightly audit run can "
                        "reference what the evening's arrivals actually did."
                    ),
                ),
            ],
            # Deliberately no user-preference strategy: guest preferences live
            # authoritatively in the foundation's `guests.preferences` JSONB and
            # are fetched per request. A second, inferred copy of them would be a
            # correctness hazard, not a feature.
            description=(
                "Hotel Operations Agent: short-term session continuity per "
                "sub-agent, plus semantic and summarization long-term strategies"
            ),
        )

        # ------------------------------------------------------------------ #
        # Code Interpreter -- A5 only
        # ------------------------------------------------------------------ #
        self.code_interpreter = agentcore.CodeInterpreterCustom(
            self,
            "HotelOpsAnalytics",
            code_interpreter_custom_name=CODE_INTERPRETER_NAME,
            # SANDBOX: no public internet. Not total isolation -- the service
            # documents that sandbox mode keeps limited access to AWS services,
            # Amazon S3 among them (docs.aws.amazon.com/bedrock-agentcore/latest/
            # devguide/code-interpreter-resource-management.html); an earlier
            # version of this comment said "no network", which was wrong. The only
            # data that should ever enter this sandbox is the metric rows A5
            # already read through the Gateway, and a code tool with internet
            # egress is a data-exfiltration path no variance calculation needs. This is also why the stack
            # always sets CODE_INTERPRETER_ID -- the service default
            # (`aws.codeinterpreter.v1`) *does* have network access.
            network_configuration=(
                agentcore.CodeInterpreterNetworkConfiguration.using_sandbox_network()
            ),
            description=(
                "Sandboxed Python for the regional performance agent: variance, "
                "trendlines and RevPAR deltas over up to 92 days x 50 properties, "
                "which is exactly the arithmetic an LLM should not do in-context"
            ),
        )

        # ------------------------------------------------------------------ #
        # Runtime -- the agent graph
        # ------------------------------------------------------------------ #
        runtime_logs = logs.LogGroup(
            self,
            "RuntimeApplicationLogs",
            # The /aws/vendedlogs/ prefix is where CloudWatch log *delivery*
            # writes; the delivery service-linked role can reach it even if the
            # managed resource policy below is ever removed.
            log_group_name="/aws/vendedlogs/hotel-ops-agent/runtime",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        environment = {
            "GATEWAY_URL": self.gateway.gateway_url,
            "MEMORY_ID": self.memory.memory_id,
            "MEMORY_SEMANTIC_NAMESPACE": FACTS_NAMESPACE,
            "MEMORY_SUMMARY_NAMESPACE": SHIFT_NAMESPACE,
            "CODE_INTERPRETER_ID": self.code_interpreter.code_interpreter_id,
            "MODEL_ID": model_id,
            "LOG_LEVEL": log_level,
        }
        # Omitted rather than set empty: unset means chain-wide, and the payload
        # must then carry propertyId for anything that reaches A1-A4.
        if property_scope:
            environment["PROPERTY_SCOPE"] = property_scope

        self.runtime = agentcore.Runtime(
            self,
            "HotelOpsRuntime",
            runtime_name=RUNTIME_NAME,
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                path=str(BUILD_DIR),
                runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
                # Direct code deploy: the zip is unpacked to the working
                # directory, which is simultaneously the source root and the
                # site-packages root. `scripts/build_agents.sh` assembles it.
                entrypoint=list(RUNTIME_ENTRYPOINT),
            ),
            # See the module docstring: IAM, not Cognito, because a runtime
            # supports one inbound method per version and every non-browser
            # caller in this system signs SigV4.
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            # The Runtime talks to Bedrock, the Gateway, Memory and the Code
            # Interpreter -- all public endpoints. It never reaches the
            # foundation's database, so a VPC would add ENIs and a NAT bill for
            # nothing.
            network_configuration=(
                agentcore.RuntimeNetworkConfiguration.using_public_network()
            ),
            environment_variables=environment,
            # Does not mean what its name suggests: it adds no property to the
            # runtime resource, it builds a TRACES *delivery* whose destination type
            # is XRAY. The CloudWatch Logs API rejects that delivery unless the
            # account's X-Ray trace segment destination has been switched to
            # CloudWatchLogs -- X-Ray Transaction Search -- so for most of this
            # project it was left unset, because that switch is account-wide and
            # would change where every traced resource in the account sends
            # segments, the foundation's 62 Lambdas included.
            #
            # It is on now because Phase 5 requires it and the account owner
            # approved the switch. AgentCore Evaluations reads *spans* from the
            # `aws/spans` log group, and that log group only exists, and only
            # receives the Runtime's spans, with Transaction Search enabled and this
            # flag set. Without both, online evaluation deploys cleanly, reports
            # ACTIVE, and silently scores nothing -- which is precisely the outcome
            # the phase exists to prevent.
            #
            # Measured before flipping it: ~136 traced requests/day account-wide,
            # with the X-Ray indexing rule at 0%, so the added CloudWatch Logs
            # ingestion is cents a month rather than the open-ended bill the
            # original comment feared.
            tracing_enabled=True,
            logging_configs=[
                agentcore.LoggingConfig(
                    destination=agentcore.LoggingDestination.cloud_watch_logs(
                        runtime_logs
                    ),
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                )
            ],
            # One runtime in this account, so the account-level resource-policy
            # quota (10 for CloudWatch Logs, lower for X-Ray) is not a concern
            # and CDK managing the policies is the simpler correct default.
            manage_delivery_resource_policy=True,
            # Defaults are already right: a chat turn is short and a night-audit
            # run is long, and the 8-hour ceiling bounds both. Stated so the
            # numbers are reviewable rather than implied.
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(15),
                max_lifetime=Duration.hours(8),
            ),
            description=(
                "Hotel Operations Agent: the orchestrator and all five "
                "sub-agents (A1 arrivals, A2 housekeeping, A3 billing, A4 night "
                "audit, A5 regional) as Strands agents-as-tools in one process"
            ),
        )

        # ------------------------------------------------------------------ #
        # Grants
        # ------------------------------------------------------------------ #
        # The construct already grants the Runtime's role its own log groups,
        # X-Ray, CloudWatch metrics, workload identity, and read on the code
        # asset. What it cannot know is which model and which AgentCore resources
        # this particular agent uses.
        for statement in self._model_invoke_statements(model_id):
            self.runtime.add_to_role_policy(statement)

        # Observed as an AccessDenied in CloudTrail on the first deploy: AgentCore
        # assumes this role and puts a resource policy on the log group it creates
        # for the runtime, `/aws/bedrock-agentcore/runtimes/{id}-{endpoint}`. The
        # construct grants CreateLogGroup/CreateLogStream/PutLogEvents on that
        # prefix but not PutResourcePolicy, so the call fails and the group is left
        # without the policy. Non-fatal -- the runtime still starts -- but it is a
        # denial in the audit trail with no reason to be there. Scoped to the
        # service's own log groups; this is not an account-level grant.
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="RuntimeLogGroupResourcePolicy",
                actions=["logs:PutResourcePolicy"],
                resources=[
                    self.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name="/aws/bedrock-agentcore/runtimes/*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
            )
        )

        self.gateway.grant_invoke(self.runtime)
        # Read *and* write, as two calls: there is no grant_read_write here. Both
        # are needed -- the session manager reloads history on every delegation
        # and flushes new events at the end of one.
        self.memory.grant_read(self.runtime)
        self.memory.grant_write(self.runtime)
        self.code_interpreter.grant_use(self.runtime)

        # ------------------------------------------------------------------ #
        # Endpoint
        # ------------------------------------------------------------------ #
        # No `version=`: the endpoint follows the version this stack just
        # deployed. See the module docstring for why pinning to "1" would be a
        # trap without a promotion process to un-pin it.
        self.production_endpoint = self.runtime.add_endpoint(
            PRODUCTION_ENDPOINT,
            description=(
                "Endpoint the ops console and the scheduled/reactive invoker "
                "target. Tracks the version deployed by hotel-ops-agent-agentcore."
            ),
        )

        # ------------------------------------------------------------------ #
        # Outputs
        # ------------------------------------------------------------------ #
        CfnOutput(
            self,
            "GatewayUrl",
            value=self.gateway.gateway_url,
            description="MCP endpoint fronting all five tool targets (SigV4)",
        )
        CfnOutput(
            self,
            "RuntimeArn",
            value=self.runtime.agent_runtime_arn,
            description="AgentCore Runtime hosting the orchestrator and A1-A5",
        )
        CfnOutput(
            self,
            "ProductionEndpointArn",
            value=self.production_endpoint.agent_runtime_endpoint_arn,
            description="Endpoint for InvokeAgentRuntime from the console and schedules",
        )
        CfnOutput(
            self,
            "MemoryId",
            value=self.memory.memory_id,
            description="AgentCore Memory id (also published to the Runtime as MEMORY_ID)",
        )
        CfnOutput(
            self,
            "CodeInterpreterId",
            value=self.code_interpreter.code_interpreter_id,
            description="Sandboxed Code Interpreter used only by the regional agent",
        )
        CfnOutput(
            self,
            "RuntimeExecutionRoleArn",
            value=self.runtime.role.role_arn,
            description="Principal that signs Gateway calls and reads/writes Memory",
        )

    # ---------------------------------------------------------------- helpers

    def _interceptor_function(
        self,
        construct_id: str,
        *,
        source: str,
        function_name: str,
        environment: dict[str, str],
        timeout: Duration,
        description: str,
    ) -> lambda_.Function:
        """One Gateway interceptor. Small, boto3-only, and on the hot path.

        No layer: neither interceptor imports anything the Lambda runtime does
        not already ship, and attaching the ``hotel_ops`` layer would give the
        approval gate a ``FoundationClient`` it has no business holding.
        """
        return lambda_.Function(
            self,
            construct_id,
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(LAMBDAS_DIR / source)),
            environment=environment,
            timeout=timeout,
            # boto3 and a JSON body. More memory would only buy CPU this does
            # not use.
            memory_size=256,
            # So an interceptor's span lands in the same trace as the tool call
            # it gated or recorded.
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

    def _model_invoke_statements(self, model_id: str) -> list[iam.PolicyStatement]:
        """``bedrock:InvokeModel`` scoped to one model. Never ``*``.

        Delegates to the module-level :func:`model_invoke_statements`, which
        ``evaluation_stack`` also uses for its judge model. The cross-region
        inference-profile subtlety below is exactly the kind of thing that must not
        be re-derived in a second place.
        """
        return model_invoke_statements(self, model_id)


def model_invoke_statements(stack: Stack, model_id: str) -> list[iam.PolicyStatement]:
    """``bedrock:InvokeModel`` scoped to one model. Never ``*``.

    A cross-region inference profile needs two grants, not one: the profile ARN in
    this account, and the foundation-model ARN in *every* region the profile can
    route to. Granting only the profile produces an AccessDenied naming a model ARN
    the caller never mentioned, and granting only ``us-east-1`` produces one that
    appears at random -- whichever region happened to serve that request.
    """
    def _foundation_model_arn(region: str, model: str) -> str:
        # Foundation models are AWS-owned, so the account segment is empty.
        return stack.format_arn(
            service="bedrock",
            region=region,
            account="",
            resource="foundation-model",
            resource_name=model,
        )

    prefix = _PROFILE_PREFIX.match(model_id)
    if not prefix:
        # A bare foundation model id: in-region only, no profile involved.
        return [
            iam.PolicyStatement(
                sid="InvokeFoundationModel",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[_foundation_model_arn(stack.region, model_id)],
            )
        ]

    bare_model_id = model_id[prefix.end() :]
    return [
        iam.PolicyStatement(
            sid="InvokeInferenceProfile",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                stack.format_arn(
                    service="bedrock",
                    resource="inference-profile",
                    resource_name=model_id,
                )
            ],
        ),
        iam.PolicyStatement(
            sid="InvokeProfileTargetModels",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                _foundation_model_arn(region, bare_model_id)
                for region in INFERENCE_PROFILE_REGIONS
            ],
        ),
    ]


# -------------------------------------------------------------------------- #
# Synth-time build check
# -------------------------------------------------------------------------- #


def _verify_agent_build() -> None:
    """Assert ``build/agents/`` is a deployable Runtime bundle.

    ``build/`` is generated and not version-controlled, so this is the only place
    that can catch a forgotten ``scripts/build_agents.sh``. The failure it
    prevents is expensive and mute: the Runtime accepts any zip, starts it,
    fails ``/ping``, and reports CREATE_FAILED some minutes later.
    """
    if not BUILD_DIR.is_dir():
        raise FileNotFoundError(
            f"{BUILD_DIR} does not exist. The AgentCore Runtime is deployed from "
            "this directory, which is generated rather than committed.\n"
            "  Run: scripts/build_agents.sh"
        )

    missing = [name for name in REQUIRED_BUILD_ARTIFACTS if not (BUILD_DIR / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"{BUILD_DIR} is not a complete Runtime bundle. Missing:\n"
            + "".join(f"  {name}\n" for name in missing)
            + "This usually means the build is stale, or that dependencies were "
            "installed for the host platform rather than vendored flat.\n"
            "  Run: scripts/build_agents.sh --clean"
        )

    # `entryPoint` is an argv: argv[0] is looked up in bin/ and exec'd. `uv pip
    # install --target` writes console scripts pointing at the *host*
    # interpreter, which does not exist on the Runtime, and a zip that loses the
    # exec bit produces a container that cannot start. build_agents.sh fixes
    # both; this asserts the fix survived.
    launcher = BUILD_DIR / "bin" / RUNTIME_ENTRYPOINT[0]
    if not launcher.is_file():
        raise FileNotFoundError(
            f"{launcher} is missing. The Runtime entrypoint is "
            f"{list(RUNTIME_ENTRYPOINT)}, so argv[0] must exist in bin/.\n"
            "  Run: scripts/build_agents.sh --clean"
        )
    mode = launcher.stat().st_mode
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise PermissionError(
            f"{launcher} is not executable. CDK's asset zipper preserves file "
            "mode, so this would deploy a Runtime that cannot exec its "
            "entrypoint.\n"
            f"  Run: chmod +x {launcher}"
        )

    shebang = launcher.read_bytes()[:64].split(b"\n", 1)[0]
    if not shebang.startswith(b"#!/usr/bin/env "):
        raise ValueError(
            f"{launcher} has a non-portable shebang: {shebang.decode(errors='replace')!r}.\n"
            "It must be '#!/usr/bin/env python3' -- the host interpreter path "
            "does not exist inside the Runtime.\n"
            "  Run: scripts/build_agents.sh --clean"
        )
