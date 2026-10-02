# Implementation Guide

Deploying the hotel operations agents, end to end, with the checks that prove each
layer works before you build the next one on top of it.

Budget about **90 minutes** for a first deployment, most of it waiting: the
AgentCore Runtime bundle is ~98 MB of arm64 wheels, and CloudFront takes a few
minutes to settle.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Two environment gotchas that will cost you an hour](#2-two-environment-gotchas-that-will-cost-you-an-hour)
3. [Deploy](#3-deploy)
4. [Verify](#4-verify)
5. [Configuration reference](#5-configuration-reference)
6. [Turning on unattended operation](#6-turning-on-unattended-operation)
7. [Turning on evaluation](#7-turning-on-evaluation)
8. [Production hardening](#8-production-hardening)
9. [Troubleshooting](#9-troubleshooting)
10. [Cost](#10-cost)
11. [Teardown](#11-teardown)

---

## 1. Prerequisites

### The platform

Deploy **[aws-samples/sample-hospitality-systems](https://github.com/aws-samples/sample-hospitality-systems)**
first, including its database migrations and its seed scripts, following its own
[DEPLOYMENT.md](https://github.com/aws-samples/sample-hospitality-systems/blob/main/DEPLOYMENT.md).
This project reads that stack's outputs at synth time and refuses to synthesize without
them.

Three conditions, all required:

- **Same account and Region.** The agents' Cognito users are created in the platform's
  user pool and the event rules are added to the platform's bus, so both projects must
  live together. Everything here defaults to `us-east-1`.
- **Seeded and signed-in once.** You need at least one property with rooms, stays and
  folios, and you should have signed in to the platform's PMS console as its seeded
  administrator. An empty platform deploys fine and gives the agents nothing to reason
  about.
- **A compatible platform version.** The routes, outputs, Cognito groups and claims, and
  event names this project uses match the public repository as of commit
  [`7fdc3fe`](https://github.com/aws-samples/sample-hospitality-systems/commit/7fdc3fe).
- **Operators are registered.** The console takes each operator's property, region or
  chain-wide scope from this project's registry, `hotel-ops-agent-staff-scope`, not from
  their token. The platform's SPA app client lets any signed-in user rewrite their own
  `custom:property_id` and `custom:region`, so those claims cannot be the authority.
  After the first deploy, register everyone with `scripts/register_staff_scope.py` (see
  §3.5). An unregistered operator is refused (`NOT_REGISTERED`), and so is one whose token
  claims a different scope from their registration (`SCOPE_MISMATCH`).

  It is still worth removing both attributes from `WriteAttributes` on the platform's SPA
  client in its `stacks/auth.yaml`: the platform's own authorizer trusts them for its own
  UI. Nothing there writes them that way — staff scope is set by its seed scripts through
  `AdminCreateUser` — so nothing breaks. The synth prints a note while it is writable.

Integrating a different CRS or PMS instead? Stop here and read the
[Pattern Extension Guide](PATTERN_EXTENSION_GUIDE.md) — the steps below assume the
reference platform.

It deploys with `sam deploy --guided`, so **you choose the stack name.** If it isn't
`anycompany-booking`, pass yours to every command in this guide:

```bash
npx cdk deploy --all -c foundationStackName=my-hospitality-stack ...
```

Confirm the five required outputs exist before going further:

```bash
aws cloudformation describe-stacks --stack-name <your-platform-stack> \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`||OutputKey==`PmsApiUrl`||OutputKey==`UserPoolId`||OutputKey==`AdminAuthClientId`||OutputKey==`UserPoolClientId`].[OutputKey,OutputValue]' \
  --output table
```

All five must be present. A missing one fails the synth with a message naming it —
deliberately, because a silent default would produce a stack that deploys cleanly and
then returns 403 on every call at runtime.

You also need at least one property seeded, and its id. Any property works:

```bash
aws cloudformation describe-stacks --stack-name <your-platform-stack> \
  --query 'Stacks[0].Outputs[?OutputKey==`PmsCloudFrontUrl`].OutputValue' --output text
# sign in to that console as the seeded admin and copy a property id
```

### Toolchain

| Tool | Version | Why |
|---|---|---|
| Python | 3.12 | The Runtime, the Lambdas, and the CDK app all target 3.12 |
| Node.js | 20 or 22 | For the CDK CLI. Node 25 works but prints an untested-version warning |
| AWS CDK CLI | ≥ 2.226 | Earlier versions lack the stable `aws_bedrockagentcore` module |
| Docker | any | Only for `pip --platform manylinux2014_aarch64`; not needed if your machine is arm64 |

### Bedrock model access

Claude Sonnet 5 must be enabled in your account, and the **cross-region inference
profile** is what the agents use:

```bash
aws bedrock get-inference-profile --inference-profile-identifier us.anthropic.claude-sonnet-5 \
  --query '{status:status,models:models[].modelArn}'
```

If that 404s, enable model access in the Bedrock console first. The stacks grant
`bedrock:InvokeModel` on the profile ARN **and** the underlying foundation-model ARN
in every region the profile can route to — granting only the profile produces an
`AccessDenied` naming a model ARN you never mentioned.

### IAM

Deployment needs to create IAM roles, Cognito users, DynamoDB tables, Lambda
functions, API Gateway, CloudFront, and AgentCore resources. Administrator access in
a development account is the simplest path. Do not deploy this from a role you would
be unhappy to see in CloudTrail creating Cognito users.

---

## 2. Two environment gotchas that will cost you an hour

Read these two. They are not stylistic.

### `--profile` does not reach the CDK app

The CDK app resolves the platform's outputs by calling `describe_stacks` from Python
at synth time. `cdk --profile X` configures the **CLI**, not the app subprocess, so
`boto3.Session()` inside the app gets no profile and fails with
`InvalidClientTokenId` — an error that reads like broken credentials and is a broken
*hand-off*.

**Always use environment variables:**

```bash
export AWS_PROFILE=<your-profile>
export AWS_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
```

### Every command needs the `--app` flag

`cdk.json` lives in `infra/`, but the asset paths resolve from the repository root.
So run every `cdk` command **from the repository root** and point it at the app
explicitly:

```bash
npx cdk deploy --all --app "infra/.venv/bin/python infra/app.py"
```

To save typing:

```bash
alias hcdk='npx cdk --app "infra/.venv/bin/python infra/app.py"'
```

Every command below assumes both of these.

---

## 3. Deploy

### 3.1 Install

```bash
git clone <this-repo> && cd hotel-operations

python3 -m venv infra/.venv
infra/.venv/bin/pip install -r infra/requirements.txt
infra/.venv/bin/pip install -r requirements-dev.txt   # pytest, for the offline suite

npm install -g aws-cdk        # or use npx, as the commands below do
```

Confirm the offline suite passes before touching AWS. It needs no credentials and
cannot reach the platform:

```bash
infra/.venv/bin/python -m pytest tests/unit -q
# 237 passed
```

### 3.2 Build the Runtime bundle

**Do this before every deploy that changes anything under `agents/`.** The bundle is
the Runtime's code asset; a source fix that is not rebuilt is not deployed. This is
the single most common way to spend twenty minutes debugging a fix that was never
shipped.

```bash
scripts/build_agents.sh
```

It vendors Linux arm64 wheels **flat** into `build/agents/` — flat because AgentCore
unzips the archive to `/var/task` and puts that directory first on `sys.path`, so a
dependency in a subdirectory would only import as `from subdir import x`.

The CDK app refuses to synthesize if `build/agents/` is missing or incomplete, and
names the missing file. Editing `agents/requirements.txt` changes its hash and forces
a full arm64 reinstall, which takes a few minutes; a source-only change reuses the
dependency layer and takes seconds.

### 3.3 Bootstrap and deploy the backend

```bash
npx cdk bootstrap --app "infra/.venv/bin/python infra/app.py"

npx cdk deploy --all \
  -c pilotPropertyIds=<your-property-uuid> \
  --app "infra/.venv/bin/python infra/app.py" \
  --require-approval never
```

Stack order is enforced by dependency; CDK will get it right. What it does:

| Stack | Time | Notes |
|---|---|---|
| `identity` | ~3 min | Creates one Cognito user per agent plus one per property for housekeeping. Discovers the property list at deploy time by calling `GET /properties`. |
| `tools` | ~2 min | Five Lambdas and a layer |
| `agentcore` | **~6 min** | The Gateway, 29 tools, Memory, Code Interpreter, and the Runtime. The Runtime is the slow part. |
| `orchestration` | ~2 min | Tables, queues, invoker, and **disabled** schedules and rules |
| `api` | ~2 min | Three Lambdas and the API Gateway |
| `evaluation` | ~2 min | Evaluators and the online config |

`frontend` is **skipped** at this point — the app only includes it when
`frontend/dist/` exists, so a backend deploy needs no Node toolchain.

### 3.4 Build and deploy the console

The build reads the Cognito ids from the **deployed** `api` stack, so it has to come
after step 3.3:

```bash
scripts/build_frontend.sh
npx cdk deploy hotel-ops-agent-frontend --app "infra/.venv/bin/python infra/app.py"
```

The console URL is the stack's `ConsoleUrl` output. CloudFront takes 3–5 minutes to
propagate; a 403 immediately after deploy usually means the distribution is still
settling, not that OAC is wrong.

Sign in with **any existing staff account from the platform's own seed data** — this
project creates no console users. The platform's `DEMO_GUIDE.md` lists them. Use a
chain-level `Admin` or `Manager` account: property-scoped accounts are pinned to
their own property and will show an empty console unless that property is in
`pilotPropertyIds`.

---

### 3.5 Register the console's operators

The console resolves every operator's scope from `hotel-ops-agent-staff-scope`, which the
api stack creates empty. Until it is populated **everyone is refused**, including the
agent identities the verification scripts sign in as.

```bash
scripts/register_staff_scope.py seed           # dry run: what it would register
scripts/register_staff_scope.py seed --apply
```

`seed` proposes one registration per account in a staff group, copied from that
account's attributes **as they are now**. Review the list before applying it: anyone who
has already rewritten their own `custom:property_id` would be registered with the value
they chose. From then on a rewrite is caught. Register later joiners one at a time with
`set`.

## 4. Verify

Run these in order. Each one assumes the previous passed, and each runs against
**deployed** resources rather than a synthesized template.

```bash
export AWS_PROFILE=<your-profile>

# Layer 1 — the tool Lambdas in isolation. No model, no Gateway.
tests/integration/verify_tools.py                    # 24/24

# Layer 2 — the agent graph against the real Gateway. --no-model skips the paid check.
tests/integration/verify_gateway.py                  # 10/10 (+2 with the model)

# Layer 5 — tables, invoker, decision log, event-rule patterns.
tests/integration/verify_orchestration.py --no-model  # 14/14 offline
tests/integration/verify_orchestration.py            # 30/30, runs one real agent

# Layer 4 — THE APPROVAL LOOP. The one that matters.
tests/integration/verify_console.py                  # 25/25

# All five agents, end to end through the queue.
tests/integration/exercise_agents.py                 # writes to the platform
tests/integration/exercise_agents.py --only a4,a5    # advisory agents only
```

**If Layer 4 fails, stop and fix it before anything else ships.** It is the check
that proves a human is genuinely required to move money, and that a released
approval cannot be spent on a different folio or a different amount.

### Non-interference

Run before and after every deploy. The platform's stack status and last-updated
timestamp must not change:

```bash
aws cloudformation describe-stacks --stack-name <your-platform-stack> \
  --query 'Stacks[0].{Status:StackStatus,Updated:LastUpdatedTime}'
aws cognito-idp list-groups --user-pool-id <UserPoolId> --query 'length(Groups)'
```

The only permitted deltas are new Cognito **users** and new EventBridge **rules**.

---

## 5. Configuration reference

All passed as `-c key=value` on `cdk deploy`.

| Key | Default | What it does |
|---|---|---|
| `foundationStackName` | `anycompany-booking` | The platform's CloudFormation stack name |
| `pilotPropertyIds` | one hardcoded uuid | **Set this.** Comma-separated properties the schedules and event rules cover |
| `enableTriggers` | `false` | Arms the Scheduler cadences and the event rules. See §6 |
| `evaluationSampling` | `10` | Percentage of sessions graded. Raise to `100` while verifying |
| `modelId` | `us.anthropic.claude-sonnet-5` | Inference profile for the agents and the judges |
| `propertyScope` | *(unset)* | Pins the whole deployment to one property. Leave unset for chain-wide |
| `apiPacingMs` | `50` | Delay between outbound platform calls, to stay under its rate limits |
| `agentEmailDomain` | `anycompany.internal` | Domain for the agent Cognito usernames |
| `identitySalt` | `1` | Bump to force agent password rotation |
| `environment` | `dev` | `dev` enables Gateway DEBUG exceptions, which let a sub-agent read a real 409 |

### Why `pilotPropertyIds` matters

The schedules are created **per property**. A1 at 30 minutes plus A2 at 20 is ~120
agent runs per property per day; across 50 properties that is ~6,000 daily runs
writing to a live database. Start with one property.

A2 also has no chain-wide mode at all — its Cognito identity is resolved per
property, so a chain-wide invocation is simply refused.

---

## 6. Turning on unattended operation

Everything ships **disabled**: four Scheduler cadences and three EventBridge rules,
all `DISABLED`. That is deliberate. Arming them starts agent runs on a timer that
write to the platform, which is a decision a deploy should not make on your behalf.

```bash
# Check current state
aws scheduler list-schedules --group-name hotel-ops-agent \
  --query 'Schedules[].[Name,State]' --output table
aws events list-rules --event-bus-name <platform-bus> --name-prefix hotel-ops \
  --query 'Rules[].[Name,State]' --output table

# Arm them
npx cdk deploy hotel-ops-agent-orchestration -c enableTriggers=true \
  -c pilotPropertyIds=<uuid> --app "infra/.venv/bin/python infra/app.py"
```

What arms:

| Trigger | Cadence | Agent |
|---|---|---|
| `hotel-ops-a1-arrivals-*` | every 30 min | A1 pre-assigns rooms |
| `hotel-ops-a2-housekeeping-*` | every 20 min | A2 sequences tasks |
| `hotel-ops-a4-nightaudit-*` | 23:45 UTC | A4 pre-audit report |
| `hotel-ops-a5-regional` | 13:00 UTC | A5 portfolio review |
| `hotel-ops-reservation-created-to-a1` | reactive | A1 on a new reservation |
| `hotel-ops-checked-out-to-a2` | reactive | A2 on a checkout |
| `hotel-ops-checked-out-to-a3` | reactive | A3 folio integrity at checkout |

To disarm, deploy again without the flag. To watch what fires:

```bash
aws logs tail /aws/lambda/hotel-ops-agent-invoker --follow
aws sqs get-queue-attributes --queue-url <InvocationDlqUrl> \
  --attribute-names ApproximateNumberOfMessages    # should always be 0
```

### A note on A3's trigger

The specification called for a reactive A3 trigger on `billing.payment_failed`.
**Nothing in the platform publishes that event** — both real payment-failure paths
update the database and log. The nearest published signal carries no `propertyId`,
and no tool resolves a folio from a reservation id, so it cannot drive a run either.
A3 is woken by `checkinout.checked_out` instead, which carries both ids and is the
moment folio integrity actually matters.

---

## 7. Turning on evaluation

Online evaluation has an **account-wide prerequisite**, and without it the config
deploys, reports `ACTIVE`, and silently scores nothing.

### CloudWatch Transaction Search

AgentCore Evaluations reads **spans**, and spans only reach CloudWatch Logs once
Transaction Search is enabled. This is account- and region-wide: it changes where
every traced resource in the region sends segments.

**Measure before you enable it.** At low trace volume the cost is negligible; at high
volume it is not:

```bash
aws xray get-trace-segment-destination                    # expect Destination: XRay
aws xray get-indexing-rules                               # 0% means no indexed-span charges
```

Then:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

# 1. Let X-Ray write to the spans log group. Use a DISTINCT policy name --
#    put-resource-policy replaces by name, and this project already owns one.
aws logs put-resource-policy \
  --policy-name HotelOpsTransactionSearchXRayAccess \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Sid\":\"TransactionSearchXRayAccess\",
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"xray.amazonaws.com\"},
      \"Action\":\"logs:PutLogEvents\",
      \"Resource\":[
        \"arn:aws:logs:$REGION:$ACCOUNT:log-group:aws/spans:*\",
        \"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/application-signals/data:*\"
      ],
      \"Condition\":{
        \"ArnLike\":{\"aws:SourceArn\":\"arn:aws:xray:$REGION:$ACCOUNT:*\"},
        \"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}
      }
    }]
  }"

# 2. Flip the destination. Takes ~6 minutes to go ACTIVE.
aws xray update-trace-segment-destination --destination CloudWatchLogs
aws xray get-trace-segment-destination     # wait for Status: ACTIVE
```

`tracing_enabled=True` is already set on the Runtime in `agentcore_stack.py`. With
Transaction Search on and ADOT ≥ 0.18, the Runtime uses **unified telemetry**: spans
land in its own log group under a new `spans` stream, which is where the evaluation
config already points.

### Verify grading

```bash
# Grade everything while checking, then put it back
npx cdk deploy hotel-ops-agent-evaluation -c evaluationSampling=100 \
  --app "infra/.venv/bin/python infra/app.py"

tests/integration/exercise_agents.py --only a1     # produces assign_room calls
sleep 600                                          # session timeout + grading
tests/integration/verify_evaluation.py             # 12/13 or better

npx cdk deploy hotel-ops-agent-evaluation --app "infra/.venv/bin/python infra/app.py"
```

Scores land in a dedicated log group **and** as CloudWatch metrics in EMF under
`Bedrock-AgentCore/Evaluations`, so dashboards and alarms need no extra plumbing.

Expect a **10–20 minute delay** to the first score: grading starts once a session has
been idle for `sessionTimeoutMinutes` (5), and the service sweeps on its own schedule.

---

## 8. Production hardening

Do these before anyone relies on the console.

### Pin the production endpoint

The `production` endpoint is created **unpinned**, so it follows whatever was
deployed last. Every `cdk deploy` of `agentcore` therefore changes what live
operators are talking to, with no promotion step.

```bash
aws bedrock-agentcore-control get-agent-runtime-endpoint \
  --agent-runtime-id <runtime-id> --endpoint-name production \
  --query '{live:liveVersion,target:targetVersion}'
# targetVersion: null  →  unpinned
```

To pin, pass a version to `add_endpoint` in `agentcore_stack.py`. Note that
**pinning without a promotion process is worse than not pinning**: version numbers
are opaque integers the service assigns on update, so you cannot pin to the version
the same deploy is about to create. A pinned endpoint means a two-step release —
deploy to create the version, then deploy again to promote it. Make the version a
context value so promotion is a flag rather than a code edit.

`DEFAULT` continues to track latest, which is where smoke tests should run.

### Other items

- **Only part of the console API has offline tests.** `properties` and the
  approval-release rules (self-approval, where the token travels) are covered;
  the rest of `chat`, `runs` and `approvals` relies on the live Layer 4 check.
- **Approval tokens are not single-use.** Nothing consumes them, deliberately: the
  Gateway may retry an interceptor, and a gate that burned the token on a retry would
  refuse a write a human did approve. Containment is the binding to the action, the
  target and every captured argument, plus a 15-minute TTL. Shorten `APPROVAL_TTL` in `api_stack.py` if you want tighter.
- **The room-quality judge's filter key is unverified.** `CreateOnlineEvaluationConfig`
  accepts any filter key without validating it, so `gen_ai.tool.name` is an educated
  guess. `verify_evaluation.py` reports how many gradings say "this was not a room
  assignment", which is how you tell.
- **The platform's dev posture is inherited, not fixed.** No MFA on staff accounts,
  permissive CORS on the platform's own APIs, WAF in count mode. This project flags
  those rather than changing them — they belong to the platform.

---

## 9. Troubleshooting

### The console says `NOT_REGISTERED` or `SCOPE_MISMATCH`

`NOT_REGISTERED`: the operator is not in `hotel-ops-agent-staff-scope`. Register them:
`scripts/register_staff_scope.py set <email> --property <uuid>` (or `--region`, or
`--chain`).

`SCOPE_MISMATCH`: their token's `custom:property_id` or `custom:region` differs from their
registration — either they changed it, or their registration is wrong.
`scripts/register_staff_scope.py check <email>` shows both side by side. Fix whichever is
wrong; never re-seed to make the error go away, because seeding copies the token's value.

### `InvalidClientTokenId` during synth

`--profile` did not reach the app subprocess. Use `export AWS_PROFILE=...`. See §2.

### `FoundationNotDeployedError`

The platform stack is missing, incomplete, or named something else. Pass
`-c foundationStackName=<yours>` and confirm all five outputs exist.

### `Cannot find build directory` / the Runtime reports `CREATE_FAILED`

`build/agents/` is missing or stale. Run `scripts/build_agents.sh`. The synth check
names the missing file.

### A fix you deployed did not take effect

If it was under `agents/`, you almost certainly forgot `scripts/build_agents.sh`.
Confirm the deployed version changed:

```bash
aws bedrock-agentcore-control get-agent-runtime-endpoint \
  --agent-runtime-id <runtime-id> --endpoint-name production --query liveVersion
```

### `UNKNOWN_TOOL` on every dispatch

The Gateway delimits tool names as `{target}___{tool}` — **three** underscores. Both
interceptors parse any run of two or more, so this only bites custom code. Confirm
what the Gateway actually advertises:

```bash
tests/integration/verify_gateway.py --no-model
```

### Every billing write is refused as `APPROVAL_UNVERIFIABLE`

The approvals table does not exist, so the gate cannot verify and therefore refuses.
That is the correct fail-closed behaviour. Deploy `hotel-ops-agent-orchestration`.

### `Authentication error - Invalid credentials` from the Gateway

The SigV4 signer signed for the wrong region. The verification scripts pin
`us-east-1` and deliberately ignore an ambient `AWS_REGION`; if you are calling the
Gateway from your own code, set the region explicitly before importing the agent
modules.

### The console returns 401 on every request

The ID token, not the access token, must go in `Authorization` — only the ID token
carries `cognito:groups` and `custom:property_id`, which are the entire authorization
model. Also check the account is `CONFIRMED` and not `FORCE_CHANGE_PASSWORD`.

### The console is empty for a property-scoped user

They are pinned to their own property by `custom:property_id`, and all the runs are
at your pilot property. Either sign in as a chain-level account or add their property
to `pilotPropertyIds`.

### Evaluation deploys but never scores

In order of likelihood: Transaction Search is not enabled (§7); the session has not
been idle long enough; sampling is below 100% and this run simply was not sampled; or
the execution role lacks `bedrock:InvokeModel` for the judge model.
`verify_evaluation.py` distinguishes these.

### A schedule fired but nothing happened

Check the invoker's log and the DLQ. A message that failed three times is sitting in
the DLQ with its full payload, which is usually enough to see why.

---

## 10. Cost

The dominant cost is **model invocations**, and it scales with how many agent runs
you allow.

| Driver | Notes |
|---|---|
| Agent runs | 5k–20k tokens each. A5 portfolio reviews are the largest |
| Schedules | ~120 runs/property/day with A1+A2 armed. This dominates everything else |
| Evaluation | Judge invocations on the sampled fraction — 10% by default |
| Always-on | AgentCore Gateway, Memory, DynamoDB on-demand, CloudFront, seven log groups. Small |
| Transaction Search | CloudWatch Logs ingestion for spans, scaling with trace volume |

Cheap ways to keep it bounded: leave the triggers disabled and invoke on demand; keep
`pilotPropertyIds` to one property; keep `evaluationSampling` at 10; and remember the
invoker's reserved concurrency (5 scheduled, 10 chat) caps how many runs can be in
flight at once.

---

## 11. Teardown

```bash
npx cdk destroy --all --app "infra/.venv/bin/python infra/app.py"
```

Read this before you run it.

**What survives on purpose:**

- **The decision-log table is `RETAIN`.** It is the audit trail of every write the
  agents made and the ground truth evaluation scores against. A `cdk destroy` must
  not be able to erase it. Delete it by hand if you mean to — and note that a
  redeploy will fail on the name conflict until you do.
- **The agent Cognito users survive.** The identity stack's `Delete` handler is a
  deliberate no-op: that pool holds thousands of real staff accounts, and a
  `cdk destroy` must never reach in and delete accounts. Remove them explicitly:

  ```bash
  aws cognito-idp list-users --user-pool-id <pool> --filter 'email ^= "agent-"' \
    --query 'Users[].Username' --output text
  # then admin-delete-user each one you want gone
  ```

**What you must clean up separately:**

```bash
# The EventBridge rules on the platform's bus are removed with the stack, but check:
aws events list-rules --event-bus-name <platform-bus> --name-prefix hotel-ops

# Transaction Search, if you enabled it and want it off
aws xray update-trace-segment-destination --destination XRay
aws logs delete-resource-policy --policy-name HotelOpsTransactionSearchXRayAccess

# Evaluation results and the spans log group are not owned by any stack
aws logs delete-log-group --log-group-name aws/spans
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/evaluations
```

**The platform itself is untouched by any of this.** That is the whole point of the
non-interference check: tearing this project down leaves the hospitality platform
exactly as it was before you deployed, minus the agent users you choose to remove.
