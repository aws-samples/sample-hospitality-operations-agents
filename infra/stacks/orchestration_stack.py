# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unattended operation: the decision log, the approval queue, and the triggers.

Three things land here, and the first two are what the rest of the system has been
waiting for.

**The two tables.** ``agentcore_stack`` already deployed both interceptors
pointing at ``hotel-ops-agent-decisions`` and ``hotel-ops-agent-approvals`` *by
name*, because a CFN reference would have made that stack depend on this one.
Until now neither table existed, which meant the audit trail wrote nothing and the
Tier-2 gate refused every billing write as ``APPROVAL_UNVERIFIABLE`` -- the
correct fail-closed answer, and also a system in which no charge could ever be
approved. Creating them here turns both on. Nothing about the interceptors
changes; they simply start finding what they were already looking for.

**The invoker.** One SQS queue, one Lambda, one DLQ. Every unattended run goes
through it, so there is exactly one place to look when a schedule did not fire.

**The triggers.** Four Scheduler cadences and three rules on the foundation's
existing bus. Adding a rule to an existing bus does not modify the foundation's
stack -- rules are independent resources -- which is why this is one of only two
permitted foundation-side deltas.

Every trigger is created DISABLED
---------------------------------
Deliberately, and not out of timidity. Enabled at the plan's cadences, A1 (30 min)
and A2 (20 min) alone are ~120 agent runs per property per day; the foundation has
50 properties, and a chain-wide fan-out would be ~6,000 runs a day against a live
database. That is a decision about money and about write volume on someone else's
system, so it is left as a switch a human throws, not a side effect of a deploy.
Set ``-c enableTriggers=true`` to arm them.

The same reasoning bounds the scope: schedules are created per *pilot* property
(``-c pilotPropertyIds=...``), not per property. A2 has no chain-wide mode at all
-- its Cognito identity is resolved per property, so a chain-wide invocation would
simply be refused -- so a per-property schedule is the only shape that works for
it regardless of cost.

What the plan asked for that does not exist
-------------------------------------------
The plan specifies a reactive A3 trigger on ``billing.payment_failed``, described
as an event with no consumer. It is not: **nothing publishes it.** Both real
payment-failure paths in the foundation (``billing/sfn_actions._mark_payment_failed``
and ``payment/stripe_webhook._handle_payment_failed``) update the database and log,
and publish nothing at all. The nearest published signal,
``payment.capture_recording_failed``, carries ``reservationId`` but no
``propertyId`` -- and no tool can find a folio from a reservation id, so an A3 run
triggered by it could not locate the folio it was woken for. A rule matching it
would look like a working guardrail and be a dead one.

So A3 is triggered on ``checkinout.checked_out`` instead, which carries both ids
and is the moment folio integrity actually matters. The event is consumed twice,
by A2 for housekeeping and A3 for billing, which is what an event bus is for.
``A3_MISSING_TRIGGER`` below records the gap so it is not rediscovered later as a
surprise. (A pre-existing instance of the same class of bug is already live in the
foundation: its ``billing-to-loyalty-dev`` rule matches ``billing.payment_processed``
from ``anycompany.billing``, which nothing publishes either.)
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_events
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from stacks.agentcore_stack import (
    DECISIONS_TABLE_NAME,
    PRODUCTION_ENDPOINT,
    AgentCoreStack,
)
from stacks.foundation_config import FoundationConfig
from stacks.tools_stack import APPROVALS_TABLE_NAME, ASSET_EXCLUDE

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_DIR = REPO_ROOT / "infra" / "lambdas"

#: Secondary index on the decision log. The console's run-history pane filters by
#: property and reads newest-first, which the base table cannot serve: its
#: partition key is ``run_id``, so "everything that happened at this hotel today"
#: would otherwise be a full scan.
DECISIONS_BY_PROPERTY_INDEX = "property_id-ts-index"

#: TTL attribute on the approvals table. The name is not free -- the deployed
#: approval interceptor reads exactly this attribute and treats a value in the past
#: as expired, so the two must agree.
APPROVAL_TTL_ATTRIBUTE = "expiresAt"

#: Secondary index the ops console's approval queue reads. Named here because
#: ``api_stack``'s handler queries it by name.
APPROVALS_BY_STATUS_INDEX = "status-createdAt-index"

#: Longer than the invoker's own timeout, which SQS requires: a message whose
#: visibility expired while the function was still running would be handed to a
#: second invocation, and two agents would work the same run.
QUEUE_VISIBILITY = Duration.minutes(16)
INVOKER_TIMEOUT = Duration.minutes(15)

#: Three attempts, then the DLQ. An agent run is expensive and partially
#: side-effecting, so this is deliberately low: a transient throttle deserves a
#: retry, a malformed payload deserves a human, and nothing deserves ten.
MAX_RECEIVE_COUNT = 3

#: A cap on how many agent runs can be in flight at once. Without it, a burst of
#: reservation.created events during a simulator run would start as many
#: concurrent Sonnet conversations as the queue could deliver.
INVOKER_MAX_CONCURRENCY = 5

#: Interactive runs get their own budget. Modest, because each one is a Sonnet
#: conversation and an ops console has a handful of operators, not thousands.
CHAT_MAX_CONCURRENCY = 10

#: Foundation events this stack subscribes to, verified against the actual
#: ``publish_event`` call sites rather than taken from the plan -- which named
#: ``crs.reservation_created`` and ``pms.checkout_completed``, neither of which is
#: a real detail type. A rule built on a name nothing publishes matches nothing,
#: and fails by being silent.
RESERVATION_CREATED = ("anycompany.reservations", "reservation.created")
CHECKED_OUT = ("anycompany.pms", "checkinout.checked_out")

#: See the module docstring: the trigger the plan asked for cannot be built.
A3_MISSING_TRIGGER = (
    "billing.payment_failed is published by nothing in the foundation. "
    "payment.capture_recording_failed is published but carries no propertyId, and "
    "no tool resolves a folio from a reservationId, so it cannot drive an A3 run. "
    "A3 is woken by checkinout.checked_out instead."
)

# --------------------------------------------------------------------------- #
# What each unattended run is asked to do
# --------------------------------------------------------------------------- #
# These are the only agent-facing prompts that live in infrastructure rather than
# in agents/prompts/, and they are short on purpose. The system prompts already
# say how each specialist works; a trigger only has to say what this run is for.
# The run context supplies propertyId and operatingDate, and tells the agent that
# nobody is reading -- so none of these has to repeat those facts or ask it not to
# ask questions.

A1_SCHEDULE_PROMPT = (
    "Pre-assign rooms for the unassigned arrivals at this property. Work the "
    "arrivals that have no room yet, and leave the ones that already do alone. "
    "Report what you assigned and anything a human has to decide."
)

A2_SCHEDULE_PROMPT = (
    "Sequence and assign the open housekeeping tasks at this property. Respect "
    "priority, task type, and the room-status state machine, and batch by floor "
    "where it costs nothing to do so. Report what you assigned."
)

A4_SCHEDULE_PROMPT = (
    "Assess night-audit readiness at this property for this operating date, "
    "before a human triggers the audit run. Flag unresolved folios, stays that "
    "should have checked out and did not, rooms stuck mid-state, and any "
    "disagreement between the daily report and the stored audit metrics. You "
    "cannot trigger the audit yourself, so the value of this run is entirely in "
    "what it tells the person who will."
)

A5_SCHEDULE_PROMPT = (
    "Review yesterday's performance across the portfolio. Identify the properties "
    "that moved most against their recent trend, in either direction, and say what "
    "the numbers do and do not support. You have no authority to change rates or "
    "availability, so be explicit that this is advisory."
)

A1_RESERVATION_PROMPT = (
    "Reservation {reservationId} was just created at this property, checking in "
    "{checkInDate} for guest {guestId} in room type {roomTypeId}. Pre-assign a "
    "room for it if a suitable one is free, and say why you chose it. If nothing "
    "suitable is available, say so rather than placing the guest badly."
)

A2_CHECKOUT_PROMPT = (
    "Room {roomNumber} ({roomId}) just checked out at this property, reservation "
    "{reservationId}. Make sure the turnover task for it exists and is sequenced "
    "and assigned sensibly against the rest of the board -- in particular against "
    "any arrival expected into that room."
)

A3_CHECKOUT_PROMPT = (
    "Reservation {reservationId} just checked out at this property. Check the "
    "folio for integrity before it settles: missing or duplicated charges, "
    "charges that look mis-posted, a balance that does not reconcile. You cannot "
    "post, void, or adjust anything on your own authority -- if something is "
    "wrong, propose the fix with the folio id, the amount, and what happens if "
    "nobody acts."
)


class OrchestrationStack(Stack):
    """The decision log, the approval queue, and every unattended trigger."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationConfig,
        agentcore: AgentCoreStack,
        pilot_property_ids: tuple[str, ...],
        enable_triggers: bool = False,
        log_level: str = "INFO",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not pilot_property_ids:
            raise ValueError(
                "pilot_property_ids is empty, so no schedule would have a property "
                "to run against. Pass -c pilotPropertyIds=<uuid>[,<uuid>...]."
            )

        self.foundation = foundation

        # ------------------------------------------------------------------ #
        # The decision log
        # ------------------------------------------------------------------ #
        self.decisions = dynamodb.Table(
            self,
            "DecisionsTable",
            table_name=DECISIONS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="run_id", type=dynamodb.AttributeType.STRING
            ),
            # `{iso8601}#{suffix}`, written by both the response interceptor and the
            # invoker's run summary. Chronological within a run by construction, so
            # a Query returns the trajectory in the order it happened.
            sort_key=dynamodb.Attribute(
                name="event_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # This table is the audit trail for every write an agent made and the
            # ground truth Phase 5's evaluators score against. RETAIN because a
            # `cdk destroy` of this stack must not be able to erase the evidence of
            # what the agents did -- the cost is that a destroy/redeploy cycle has
            # to adopt or rename the orphan, which is a loud failure rather than a
            # silent loss.
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                )
            ),
        )
        self.decisions.add_global_secondary_index(
            index_name=DECISIONS_BY_PROPERTY_INDEX,
            partition_key=dynamodb.Attribute(
                name="property_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="ts", type=dynamodb.AttributeType.STRING),
            # Everything, because the console's run history shows the recommendation
            # and the action taken in the same list it filters by property. A
            # KEYS_ONLY index would turn one query into a query plus N GetItems.
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------ #
        # The approval queue
        # ------------------------------------------------------------------ #
        self.approvals = dynamodb.Table(
            self,
            "ApprovalsTable",
            table_name=APPROVALS_TABLE_NAME,
            # The approval id *is* the token the agent presents. So it must be
            # unguessable -- minted by the ops console API in Phase 4, never by an
            # agent, and never derived from the folio or the amount.
            partition_key=dynamodb.Attribute(
                name="approvalId", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # DynamoDB deletes expired items lazily, up to 48 hours late, so the
            # interceptor checks this attribute itself rather than trusting the
            # sweeper. The TTL is here to stop the table growing, not to enforce
            # the deadline.
            time_to_live_attribute=APPROVAL_TTL_ATTRIBUTE,
            # Unlike the decision log, nothing here is evidence: an approval is a
            # short-lived token that either was used or expired, and the *record*
            # of the approved write lives in the decision log.
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.approvals.add_global_secondary_index(
            index_name=APPROVALS_BY_STATUS_INDEX,
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="createdAt", type=dynamodb.AttributeType.STRING
            ),
            # The console's approval queue is "everything still PENDING, newest
            # first", which the base table cannot answer: it is partitioned by the
            # token, which is the one thing a human reading the queue does not have.
            #
            # Note what this index does *not* project. The partition key of the base
            # table is the approval token itself, and DynamoDB includes the base
            # table's key in every index -- so a KEYS_ONLY or INCLUDE index would
            # still carry the token, and the console's list route strips it in code.
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ------------------------------------------------------------------ #
        # The invocation queue
        # ------------------------------------------------------------------ #
        self.dlq = sqs.Queue(
            self,
            "InvocationDlq",
            queue_name="hotel-ops-agent-invocations-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            # The maximum. A schedule that broke on Friday night should still be
            # diagnosable on Monday, and these messages are the only record of what
            # was asked for.
            retention_period=Duration.days(14),
        )

        self.queue = sqs.Queue(
            self,
            "InvocationQueue",
            queue_name="hotel-ops-agent-invocations",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=QUEUE_VISIBILITY,
            retention_period=Duration.days(1),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=MAX_RECEIVE_COUNT, queue=self.dlq
            ),
        )

        #: The ops console's chat runs, consumed by the same invoker. A separate
        #: queue rather than a shared one because this is the only place in the
        #: system where a person is waiting: an operator's question must not sit
        #: behind a ten-minute A5 portfolio review that a timer started.
        self.chat_queue = sqs.Queue(
            self,
            "ChatQueue",
            queue_name="hotel-ops-agent-chat",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=QUEUE_VISIBILITY,
            # Shorter than the scheduled queue's day: a chat message nobody
            # delivered within an hour is a message whose asker has gone home.
            retention_period=Duration.hours(1),
            dead_letter_queue=sqs.DeadLetterQueue(
                # One retry, not three. A scheduled run is worth retrying blind
                # because nothing is watching; a human's question that failed twice
                # is better surfaced as an error they can see and reword.
                max_receive_count=2,
                queue=self.dlq,
            ),
        )

        # ------------------------------------------------------------------ #
        # The invoker
        # ------------------------------------------------------------------ #
        self.invoker = lambda_.Function(
            self,
            "AgentInvokerFn",
            function_name="hotel-ops-agent-invoker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(
                str(LAMBDAS_DIR / "agent_invoker"), exclude=ASSET_EXCLUDE
            ),
            environment={
                "RUNTIME_ARN": agentcore.runtime.agent_runtime_arn,
                "ENDPOINT_NAME": PRODUCTION_ENDPOINT,
                "DECISIONS_TABLE": self.decisions.table_name,
                "LOG_LEVEL": log_level,
            },
            # The Lambda maximum. Measured runs are 30-210s, but a night audit
            # reading a full day of folios is the long tail, and a run killed at
            # the timeout has already spent the tokens.
            timeout=INVOKER_TIMEOUT,
            # No dependencies beyond boto3, and the work is a single long HTTP
            # read. Memory buys nothing here.
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "AgentInvokerLogs",
                log_group_name="/aws/lambda/hotel-ops-agent-invoker",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description=(
                "Drains the invocation queue by calling the agent Runtime's "
                f"'{PRODUCTION_ENDPOINT}' endpoint, then records the "
                "run's answer in the decision log"
            ),
        )

        # Written out rather than using `runtime.grant_invoke_runtime()`, which
        # grants the runtime ARN *and* `runtime-arn/*`. That wildcard covers every
        # endpoint including DEFAULT, which tracks whatever was deployed last -- so
        # an unattended run could silently execute an untested build. Both ARNs are
        # listed because the call carries a qualifier, and the endpoint is the
        # resource on the wire while the runtime is the resource that owns it.
        self.invoker.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeProductionEndpointOnly",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    agentcore.runtime.agent_runtime_arn,
                    agentcore.production_endpoint.agent_runtime_endpoint_arn,
                ],
            )
        )
        self.invoker.add_to_role_policy(
            iam.PolicyStatement(
                sid="WriteRunSummary",
                # PutItem only, like the response interceptor. The invoker has no
                # business reading other runs and none deleting any.
                actions=["dynamodb:PutItem"],
                resources=[self.decisions.table_arn],
            )
        )

        self.invoker.add_event_source(
            lambda_events.SqsEventSource(
                self.queue,
                # One run per invocation. An agent run is minutes long and
                # side-effecting; batching them would mean a timeout part-way
                # through the batch redelivered work that had already happened.
                batch_size=1,
                report_batch_item_failures=True,
                max_concurrency=INVOKER_MAX_CONCURRENCY,
            )
        )
        self.invoker.add_event_source(
            lambda_events.SqsEventSource(
                self.chat_queue,
                batch_size=1,
                report_batch_item_failures=True,
                # Its own concurrency budget, so scheduled work saturating the
                # scheduled mapping cannot starve interactive runs. Two mappings on
                # one function is what makes that separation real -- a single queue
                # with a priority attribute would not.
                max_concurrency=CHAT_MAX_CONCURRENCY,
            )
        )

        # ------------------------------------------------------------------ #
        # Cadences
        # ------------------------------------------------------------------ #
        self.schedule_group = scheduler.ScheduleGroup(
            self,
            "ScheduleGroup",
            schedule_group_name="hotel-ops-agent",
            # The group is ours and holds nothing but these schedules, so removing
            # it with the stack takes no one else's schedules with it.
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.schedules: dict[str, scheduler.Schedule] = {}
        for property_id in pilot_property_ids:
            short = property_id.split("-")[0]
            self._schedule(
                f"A1Arrivals{short}",
                f"hotel-ops-a1-arrivals-{short}",
                # 30 minutes: the plan's cadence. Pre-assignment is not urgent
                # work -- a reservation created now checks in tomorrow -- so this
                # is about bounded staleness, not latency.
                scheduler.ScheduleExpression.rate(Duration.minutes(30)),
                A1_SCHEDULE_PROMPT,
                property_id,
                enabled=enable_triggers,
                description="A1: pre-assign rooms for upcoming arrivals",
            )
            self._schedule(
                f"A2Housekeeping{short}",
                f"hotel-ops-a2-housekeeping-{short}",
                # 20 minutes, the tightest cadence in the system: a dirty room is
                # blocking an arrival right now, so staleness here is measured
                # against a guest standing at the desk.
                scheduler.ScheduleExpression.rate(Duration.minutes(20)),
                A2_SCHEDULE_PROMPT,
                property_id,
                enabled=enable_triggers,
                description="A2: sequence and assign housekeeping tasks",
            )
            self._schedule(
                f"A4NightAudit{short}",
                f"hotel-ops-a4-nightaudit-{short}",
                # 23:45 UTC, deliberately *before* midnight rather than after. A4
                # advises the human who triggers the audit, so it has to run while
                # there is still time to act -- and running before the date rolls
                # means the run's own operatingDate is the day being closed, with
                # no date arithmetic anywhere to get wrong.
                scheduler.ScheduleExpression.cron(minute="45", hour="23"),
                A4_SCHEDULE_PROMPT,
                property_id,
                enabled=enable_triggers,
                description="A4: night-audit readiness, before the human triggers it",
            )

        self._schedule(
            "A5Regional",
            "hotel-ops-a5-regional",
            # Once, mid-morning UTC, after the whole portfolio has closed the
            # previous day. A5 is the only chain-wide cadence, which is why it is
            # outside the per-property loop.
            scheduler.ScheduleExpression.cron(minute="0", hour="13"),
            A5_SCHEDULE_PROMPT,
            None,
            enabled=enable_triggers,
            description="A5: cross-property performance review (advisory)",
        )

        # ------------------------------------------------------------------ #
        # Reactive rules on the foundation's existing bus
        # ------------------------------------------------------------------ #
        # from_event_bus_arn, not a new EventBus: this attaches to the foundation's
        # bus without CDK owning it, so nothing here can rename, re-tag, or delete
        # it. Rules are separate resources, so adding one leaves the foundation's
        # own stack untouched -- which is the whole basis for doing this at all.
        bus = events.EventBus.from_event_bus_arn(
            self, "FoundationBus", foundation.event_bus_arn
        )

        self.rules: dict[str, events.Rule] = {}
        self._rule(
            "ReservationCreatedToA1",
            "hotel-ops-reservation-created-to-a1",
            bus,
            RESERVATION_CREATED,
            pilot_property_ids,
            A1_RESERVATION_PROMPT,
            {
                "reservationId": "$.detail.reservationId",
                "guestId": "$.detail.guestId",
                "roomTypeId": "$.detail.roomTypeId",
                "checkInDate": "$.detail.checkInDate",
            },
            enabled=enable_triggers,
            description="A1 reacts to a new reservation by pre-assigning a room",
        )
        self._rule(
            "CheckedOutToA2",
            "hotel-ops-checked-out-to-a2",
            bus,
            CHECKED_OUT,
            pilot_property_ids,
            A2_CHECKOUT_PROMPT,
            {
                "reservationId": "$.detail.reservationId",
                "roomId": "$.detail.roomId",
                "roomNumber": "$.detail.roomNumber",
            },
            enabled=enable_triggers,
            description="A2 reacts to a checkout by sequencing the turnover",
        )
        self._rule(
            "CheckedOutToA3",
            "hotel-ops-checked-out-to-a3",
            bus,
            CHECKED_OUT,
            pilot_property_ids,
            A3_CHECKOUT_PROMPT,
            {"reservationId": "$.detail.reservationId"},
            enabled=enable_triggers,
            description=(
                "A3 reacts to a checkout by checking folio integrity. Note: the "
                "plan's billing.payment_failed trigger does not exist -- "
                + A3_MISSING_TRIGGER
            ),
        )

        # ------------------------------------------------------------------ #
        # Outputs
        # ------------------------------------------------------------------ #
        CfnOutput(
            self,
            "DecisionsTableName",
            value=self.decisions.table_name,
            description="Decision log: every tool call and every run summary",
        )
        CfnOutput(
            self,
            "ApprovalsTableName",
            value=self.approvals.table_name,
            description="Tier-2 approval tokens, minted by the ops console API",
        )
        CfnOutput(
            self,
            "InvocationQueueUrl",
            value=self.queue.queue_url,
            description="Send an invocation payload here to run the agent graph",
        )
        CfnOutput(
            self,
            "ChatQueueUrl",
            value=self.chat_queue.queue_url,
            description="Interactive runs from the ops console's POST /chat",
        )
        CfnOutput(
            self,
            "InvocationDlqUrl",
            value=self.dlq.queue_url,
            description="Runs that failed three times. Should always be empty.",
        )
        CfnOutput(
            self,
            "TriggersEnabled",
            value=str(enable_triggers).lower(),
            description=(
                "Whether the schedules and rules are armed. Deploy with "
                "-c enableTriggers=true to arm them."
            ),
        )

    # ---------------------------------------------------------------- helpers

    def _schedule(
        self,
        construct_id: str,
        schedule_name: str,
        expression: scheduler.ScheduleExpression,
        prompt: str,
        property_id: str | None,
        *,
        enabled: bool,
        description: str,
    ) -> scheduler.Schedule:
        """One cadence, as a message on the invocation queue.

        Scheduler could invoke the Lambda directly. It sends to the queue instead so
        that a scheduled run and a reactive one are the same thing by the time
        anything executes: one retry policy, one DLQ, one set of logs.
        """
        payload: dict[str, str] = {"prompt": prompt, "trigger": "schedule"}
        if property_id:
            payload["propertyId"] = property_id
        # operatingDate is deliberately absent: the invoker stamps it at delivery.
        # A date baked into the schedule input would be the date the stack was
        # deployed, and would be wrong from the second firing onwards.

        schedule = scheduler.Schedule(
            self,
            construct_id,
            schedule=expression,
            schedule_name=schedule_name,
            schedule_group=self.schedule_group,
            enabled=enabled,
            target=scheduler_targets.SqsSendMessage(
                self.queue,
                input=scheduler.ScheduleTargetInput.from_object(payload),
                # Scheduler's own retry, before the message is even enqueued. Low,
                # because a failure to enqueue is almost always a permissions or
                # throttling problem that the next firing will hit again anyway.
                retry_attempts=2,
                max_event_age=Duration.minutes(5),
                dead_letter_queue=self.dlq,
            ),
            description=description,
        )
        self.schedules[schedule_name] = schedule
        return schedule

    def _rule(
        self,
        construct_id: str,
        rule_name: str,
        bus: events.IEventBus,
        source_and_type: tuple[str, str],
        property_ids: tuple[str, ...],
        prompt_template: str,
        paths: dict[str, str],
        *,
        enabled: bool,
        description: str,
    ) -> events.Rule:
        """One reactive rule, filtered to the pilot properties.

        The prompt is built by EventBridge's own input transformer rather than by
        the invoker. That keeps every foundation event shape out of our Lambda --
        the invoker never learns what a ``reservation.created`` looks like -- and it
        makes the exact payload visible in the rule definition, where a human
        debugging a surprising run will actually look.
        """
        source, detail_type = source_and_type
        rule = events.Rule(
            self,
            construct_id,
            rule_name=rule_name,
            event_bus=bus,
            enabled=enabled,
            description=description,
            event_pattern=events.EventPattern(
                source=[source],
                detail_type=[detail_type],
                # Filtered at the bus, not in the agent. An unfiltered rule would
                # start an agent run for all 50 properties every time the activity
                # simulator ran, and the runs for the other 49 would be correct,
                # expensive, and unwanted.
                detail={"propertyId": list(property_ids)},
            ),
        )
        rule.add_target(
            events_targets.SqsQueue(
                self.queue,
                message=events.RuleTargetInput.from_object(
                    {
                        "prompt": _templated(prompt_template, paths),
                        # Both of these render as *unquoted* placeholders, because
                        # CDK inlines a value that is nothing but a token. That is
                        # only safe if the paths always resolve -- an unresolved one
                        # would leave malformed JSON -- and both do: `propertyId` is
                        # in the event pattern above, so EventBridge cannot match an
                        # event lacking it, and the foundation's `publish_event` and
                        # `publish_events` both set `_metadata.correlationId`
                        # unconditionally before every PutEvents.
                        "propertyId": events.EventField.from_path("$.detail.propertyId"),
                        "trigger": "event",
                        # The event's own correlation id, so a run in the decision log
                        # traces back to the foundation event that caused it.
                        "runId": events.EventField.from_path(
                            "$.detail._metadata.correlationId"
                        ),
                    }
                ),
                dead_letter_queue=self.dlq,
            )
        )
        self.rules[rule_name] = rule
        return rule


def _templated(template: str, paths: dict[str, str]) -> str:
    """Fill a prompt template with ``EventField`` references.

    ``RuleTargetInput.from_object`` resolves an ``EventField`` anywhere in the
    object, including mid-string, into EventBridge's ``<placeholder>`` syntax. So
    this produces one prompt string whose values EventBridge substitutes at
    delivery time.
    """
    return template.format(
        **{name: events.EventField.from_path(path) for name, path in paths.items()}
    )
