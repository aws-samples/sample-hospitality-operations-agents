# Pattern Extension Guide

Plugging these agents into a hospitality platform other than
[aws-samples/sample-hospitality-systems](https://github.com/aws-samples/sample-hospitality-systems):
a different central reservation system (CRS), a different property management system
(PMS), or one of each from different vendors.

This sample is built against one reference platform, but very little of it is about that
platform. The orchestrator, the five specialists, the Gateway, the approval gate, the
decision log, the console and the evaluators know nothing about how the reference
platform is implemented. They know a **tool contract**: 29 tools with fixed names, fixed
argument shapes and fixed meanings. Everything platform-specific lives beneath that
contract, in five places you can name. This guide is about those five places, and about
the rules you should keep when you rewrite them.

---

## Contents

1. [What is portable and what is not](#1-what-is-portable-and-what-is-not)
2. [Is your platform a fit?](#2-is-your-platform-a-fit)
3. [The design rules to keep](#3-the-design-rules-to-keep)
4. [The five seams](#4-the-five-seams)
5. [Identity: the hard part](#5-identity-the-hard-part)
6. [When your platform lacks a capability](#6-when-your-platform-lacks-a-capability)
7. [Mixing a CRS and a PMS from different vendors](#7-mixing-a-crs-and-a-pms-from-different-vendors)
8. [A worked sequence: replacing the PMS](#8-a-worked-sequence-replacing-the-pms)
9. [What not to change](#9-what-not-to-change)

---

## 1. What is portable and what is not

```
┌──────────────────────────────────────────────────────────────────────┐
│  Ops console · approvals · run history · evaluation      PORTABLE    │
├──────────────────────────────────────────────────────────────────────┤
│  Orchestrator + A1–A5 agents, prompts, Memory, Code Interpreter      │
│                                                           PORTABLE   │
├──────────────────────────────────────────────────────────────────────┤
│  AgentCore Gateway · 29 tools · approval + decision-log  PORTABLE    │
│  interceptors                               (this is the contract)   │
╞══════════════════════════════════════════════════════════════════════╡
│  Tool Lambdas: tools/*/handler.py            ADAPTER — you rewrite   │
│  HTTP + auth:  tools/layer/.../foundation_client.py                  │
│  Identity:     infra/stacks/identity_stack.py                        │
│  Events:       orchestration_stack.py rule constants                 │
│  Discovery:    infra/stacks/foundation_config.py                     │
├──────────────────────────────────────────────────────────────────────┤
│  Your CRS / PMS                                     UNMODIFIED       │
└──────────────────────────────────────────────────────────────────────┘
```

The double line is the tool contract. Above it, nothing needs to change as long as each
tool still means what it meant. Below it, you are writing an **adapter**: code that
translates the contract into your platform's calls and your platform's answers back into
the contract.

That split exists on purpose, not by accident. The tool Lambdas are the only code in this
project that talk to a hotel system, which is what makes them replaceable.

---

## 2. Is your platform a fit?

The agents need to *read* enough to reason and, for the two that act, *write* through an
API. Walk this list against your platform before writing code. Each row names the agent
that depends on it and what happens if it is missing — most gaps downgrade an agent
rather than remove it (see [§6](#6-when-your-platform-lacks-a-capability)).

| Capability | Needed by | If missing |
|---|---|---|
| List properties, with names | console picker, A5 | You supply a static property directory |
| Arrivals in a date window, with room assignment state | A1 | A1 cannot run |
| Guest profile: loyalty tier, preferences, accessibility needs | A1 | A1 assigns on room features alone, and says so |
| Room inventory with floor, features, connecting rooms | A1 | A1 cannot improve on the platform's default pick |
| **Write:** assign a room to a stay | A1 | A1 becomes advise-only |
| Housekeeping tasks with status, priority, room | A2 | A2 cannot run |
| **Write:** assign, complete, inspect a task | A2 | A2 becomes advise-only |
| Folios and their charges | A3, A4 | A3 cannot run |
| **Write:** post a charge, void, adjust loyalty | A3 | A3 finds problems and a human fixes them by hand |
| Daily/night-audit summary for a property | A4 | A4 works from folios and stays alone |
| Occupancy and revenue across properties, over a date range | A5 | A5 cannot run |
| An authorization model that can scope a caller to a property | all | See [§5](#5-identity-the-hard-part) — this one matters most |
| Events on booking created and guest checked out | reactive triggers | Poll on a schedule instead |

Two things that are **not** needed, because the agents deliberately do not use them:
direct database access, and any write to rates or availability.

---

## 3. The design rules to keep

These are the rules that make the reference implementation safe. They are
platform-independent, and they are the part of this sample worth copying even if you
copy nothing else. Source files cite them by section number.

### 3.1 The agent is a staff member

Each agent authenticates to the platform **as its own identity, scoped the way a human in
that role would be scoped** — the housekeeping agent as a housekeeper at one property, the
billing agent as a manager. It is never a superuser, and no agent is given the
platform's administrator role.

This is the design's strongest property, and it is worth being precise about why:

- **The platform's authorization applies unchanged.** No new authorization logic means no
  new authorization bugs. If a prompt injection convinces the housekeeping agent to post
  a charge, the *platform* refuses it, because housekeepers cannot post charges.
- **The platform's audit trail is automatic.** Every write already records who made it.
  With a dedicated identity, "who did this" answers itself.
- **Rollback is a permissions change**, not a deployment.

If your platform cannot express this — for example, it offers only one integration
credential with full access — read [§5](#5-identity-the-hard-part) before anything else.

### 3.2 Tools are existing endpoints behind one thin wrapper

The tool layer is deliberately thin. Each tool calls an endpoint the platform already has
and returns what came back. The platform owns the business rules, and copying them into
the tool layer is how the two drift apart. Three rules for the wrapper, enforced in
`tools/layer/python/hotel_ops/foundation_client.py`:

1. **Return the platform's error verbatim.** A model reasons better about
   `{"code": "INVALID_STATE", "message": "Task is already ASSIGNED"}` than about "the call
   failed". If your platform's errors are unstructured, normalise the *shape* into
   `{"success": false, "error": {"code", "message", "details"}}` but keep the *words*.
2. **Never reach the database.** Going round the API skips its authorization, its
   validation and its audit trail — the three things rule 3.1 relies on. This holds even
   when you have read access to a replica and the API is slow.
3. **Reads before writes, and a conflict is success.** Read the current state, confirm it
   still calls for the write, then write. Treat a `409` (or your platform's equivalent) as
   *someone else already did it* — usually a person at the front desk — not as a retry
   trigger.

Add a fourth for commercial platforms: **pace the calls.** The reference client sleeps
between requests. Vendor APIs usually meter per integration, and an agent that loops over
fifty rooms will find the limit in minutes.

### 3.3 Three tiers of authority

Not every write deserves the same gate.

| Tier | What | Gate | Agents |
|---|---|---|---|
| **1 · auto-execute** | Reorganising work: room pre-assignment, task sequencing and assignment | None. Reversible, no guest or money impact, and a person is already the fallback | A1, A2 |
| **2 · propose-and-confirm** | Money: charges, voids, loyalty adjustments | A person releases each one in the console | A3 |
| **3 · advise-only** | Anything with no write endpoint, or with too much blast radius | Ranked recommendation with reasoning | A4, A5 |

The rule underneath: **an agent may reorganise work freely and must never move money on
its own.** Decide your platform's tier for each write *before* you write its adapter, and
put every Tier-2 tool in the approval interceptor's list
(`infra/lambdas/approval_interceptor/index.py`, `GATED_TOOLS`). The interceptor fails
closed: a Tier-2 tool it cannot verify an approval for is refused.

An approval is bound to **the action, the target, and every argument the proposal captured** — and an argument it did not capture may not be added. All of it is checked in the
interceptor *and* again in the tool Lambda. If your platform identifies a folio
differently — a confirmation number, a composite key — bind to that. Do not relax the
binding to make an adapter easier to write; an approval bound to the action alone lets one
approval for folio A pay out on folio B.

### 3.4 Two kinds of record

Traces answer *what happened*. The decision log (`hotel-ops-agent-decisions`, one row per
tool call, retained on stack deletion) answers *was the agent right* — and it is what the
evaluators and the console's "I overrode it" button write against. Neither depends on
the platform, so both carry over untouched.

---

## 4. The five seams

Everything below the tool contract, in the order you would touch it.

### Seam 1 · Discovery — `infra/stacks/foundation_config.py`

**What it does today.** At synth time, reads five outputs from the reference platform's
CloudFormation stack (`ApiUrl`, `PmsApiUrl`, `UserPoolId`, `AdminAuthClientId`,
`UserPoolClientId`) into a frozen `FoundationConfig`, and fails the synth if any is
missing.

**What you change.** Your platform is probably not a CloudFormation stack in your
account. The module already supports a second source: point
`HOTEL_OPS_FOUNDATION_JSON` at a JSON file with the same field names. Replace the
Cognito fields with whatever your identity model needs (see
[§5](#5-identity-the-hard-part)), and keep the "fail loudly" behaviour — a stack that
deploys with a wrong base URL and 403s at runtime is much harder to diagnose than a
failed synth.

Keep secrets out of this file. It holds locations, not credentials.

### Seam 2 · Transport and identity — `foundation_client.py` and `identity_stack.py`

**What it does today.** `FoundationClient` signs in as the agent's Cognito user with
`AdminInitiateAuth`, caches the **ID token** (not the access token — only the ID token
carries the group and property claims the platform authorizes on), sends it as a bearer
token, retries once on a `401` with a fresh token, and paces every call.
`identity_stack.py` creates those Cognito users in the platform's existing pool through a
custom resource whose `Delete` is a deliberate no-op.

**What you change.** Replace `token()` and `_request()` with your platform's
authentication, and keep the rest of the class: the `{"status", "data", "ok"}` return
shape, the single reactive refresh, the pacing, and the `surface` argument (`"crs"` or
`"pms"`), which is what lets one client talk to two systems. Replace the identity stack's
provisioner with however your platform issues integration identities — often a manual
step in a vendor portal, in which case the stack only creates the Secrets Manager
secrets and you fill them in.

Credentials stay in **Secrets Manager, one secret per agent**, and each tool Lambda's role
can read only its own. That is what keeps the housekeeping Lambda from borrowing the
billing agent's identity even if its code is changed.

### Seam 3 · Tool adapters — `tools/*/handler.py` and `schemas/*.json`

**What it does today.** Five Lambdas, one per agent domain, each registering its tools
with a small router (`tools/layer/python/hotel_ops/tool_dispatch.py`). Each tool is
typically ten to thirty lines: validate arguments, call one or two endpoints, return the
envelope. The JSON schemas are what the Gateway advertises to the model.

**What you change.** The handler bodies. **Keep the tool names, the argument names and
the meaning of the response** — the prompts in `agents/prompts/` name tools directly, and
the evaluators grade against them. Where your platform's answer has a different shape,
translate it in the handler rather than changing the schema.

Some tools do more than forward a call, because the reference platform's endpoint
doesn't quite answer the question the agent is asking. `list_arrivals` is the clearest
example: the platform's `GET /stays?date=D` means *in house on D*, not *arriving on D*, so
the tool filters client-side over a window. Expect to find the same kind of mismatch in
your platform. When you do, fix it in the adapter and leave a comment saying why, and
tell the model in the tool's response what the numbers mean — the reference
`occupancy` tool attaches a `meaning` block for exactly this reason.

Two things to preserve in every adapter:

- **Property scoping is enforced twice, and neither half trusts the model.** The
  Gateway's request interceptor pins every call's `propertyId` to the run's property,
  which the Runtime sends as a header the model cannot touch, and in a human's run it
  refuses any tool that person's platform groups could not call (`TOOL_GROUPS` in
  `infra/lambdas/approval_interceptor/index.py`). Then the adapter proves that any
  record named only by id — a folio, a reservation, a room — really is at that
  property, because a chain-level identity passes the platform's own property check
  for everything. When you port the adapters, keep the second half: it is the part
  that knows how *your* platform says where a record lives.
- **Money-moving tools re-check the approval binding themselves**, independently of the
  Gateway interceptor. `tools/billing/handler.py` shows the pattern.

### Seam 4 · Events — `infra/stacks/orchestration_stack.py`

**What it does today.** Two constants name the reference platform's events —
`RESERVATION_CREATED` and `CHECKED_OUT`, each a `(source, detail-type)` pair — and three
EventBridge rules on the platform's existing bus route them to A1, A2 and A3. Every rule
and every schedule sends to the **same SQS queue**, so reactive, scheduled and chat runs
all take one path: one retry policy, one dead-letter queue, one run summary.

**What you change.** Only how events *arrive*. Three options, best first:

1. **Your platform publishes to EventBridge** (natively or through a partner source).
   Change the two constants and the `detail.propertyId` path in the rule's input
   transformer.
2. **Your platform sends webhooks.** Put an API Gateway route and a small Lambda in front
   of the same queue, verify the webhook's signature there, and translate the payload
   into the queue's message shape (`prompt`, `trigger`, `propertyId`).
3. **Your platform has no events.** Skip the rules. The four EventBridge Scheduler
   cadences already cover A1, A2, A4 and A5 on their own; the agents look for work on
   each run instead of being told about it.

Whichever you use, keep the queue. It is what makes an unattended run debuggable at
three in the morning.

Everything here ships **disabled**. Turning a trigger on means agents running and writing
against a live system on a timer; that is a decision for whoever operates the property,
not for a deploy.

### Seam 5 · The console's view of the platform — `infra/lambdas/console/`

**What it does today.** Two dependencies on the reference platform:

- The console's API Gateway authorizer trusts the **platform's own Cognito pool**, so
  hotel staff sign in with the accounts they already have, and their group and property
  claims decide what they may see and release.
- `GET /properties` forwards the operator's own token to the platform's
  `GET /properties`, which returns the properties that operator may act on.

**What you change.** If your staff sign in somewhere else, give the console its own
Amazon Cognito user pool **federated to your identity provider** (SAML or OIDC), and map
the attributes the handlers read: groups to `cognito:groups`, a property assignment to
`custom:property_id`, a region to `custom:region`. `infra/lambdas/console/layer/.../api.py`
(`Caller`) is the one place those claims are read.

For the property list, if your platform has no per-caller endpoint, serve it from a
static directory and filter it by the caller's claims in the Lambda. That moves an
authorization decision into this project, so test it like one:
`tests/unit/test_console_properties.py` shows which cases matter.

---

## 5. Identity: the hard part

Most of the rewrite is mechanical. This part is not, because the reference design leans
on something your platform may not offer.

### 5.1 When the platform can scope for you

If your platform can issue an identity that is **scoped to a role and a property** — a
staff account, an integration user with a role, an OAuth client with property-bound
scopes — do what the reference does. One identity per agent domain, least privilege for
each, and one per property for any role the platform scopes that way (the reference
creates one housekeeping user per property for this reason). You keep rule 3.1 intact,
and you are finished with this section.

### 5.2 When the platform cannot scope for you

Many commercial APIs issue one **integration credential** with access to every property
the integration is licensed for, and no notion of role. That credential cannot say "a
housekeeper at property X". The platform will no longer refuse the housekeeping agent's
attempt to post a charge; something in this project has to.

Move the missing checks into the adapters, and make them boring:

- **Split the credential by blast radius where you can.** If the vendor lets you create
  several integrations, create one for reads and one for writes, and give only the
  Tier-1 and Tier-2 Lambdas the write credential.
- **Each tool Lambda allows a fixed set of operations.** The router in
  `tool_dispatch.py` already rejects a tool that is not registered on that target; keep
  each target's tool list minimal, because that list *is* now the agent's permission set.
- **Scope properties explicitly.** The reference already pins each run to one property
  in the Gateway and has the adapters verify record ownership (see seam 3); with a
  broad credential those checks carry the whole load, so test them like the
  authorization code they now are. For a deployment that should only ever touch a few
  hotels, also give each tool Lambda an allowlist of property ids and refuse anything
  outside it.
- **Keep the Tier-2 gate exactly as it is.** With a broad credential, the approval
  interceptor and the billing Lambda's own binding check are the *only* controls between
  the model and money movement. Do not merge them, and do not loosen either.
- **Write the agent's identity into the platform's audit fields**, if it has any (a
  "user" or "source" field on the charge or task), so a person reading the PMS can still
  tell an agent's write from a colleague's.

Be explicit in your own documentation that you made this trade. It is a reasonable one,
but the resulting design is weaker than 3.1, and the next person to extend it should know
where its controls moved to.

### 5.3 Human operators in the console

The copilot runs a chat question **with the operator's own authority**: the chat Lambda
records the operator's groups, and the run context tells the orchestrator what the person
asking may and may not do. Whatever identity provider your staff use, the console must
end up with the same three facts per operator — what groups they are in, which property
(if any) they are bound to, and which region (if any). Without them, the console cannot
tell a manager who may release a charge from a front-desk agent who may not.

**Check that the scope facts cannot be written by the person they describe.** The
reference platform's own app client lets a signed-in user rewrite their
`custom:property_id`, so the reference console does not trust that claim: it reads each
operator's scope from a registry this project owns (`hotel-ops-agent-staff-scope`,
written only with IAM credentials by `scripts/register_staff_scope.py`) and refuses a
token that disagrees. Groups are taken from the token, because a user cannot change their
own. Before you reuse either, find out which of your identity provider's attributes a user
can edit about themselves.

---

## 6. When your platform lacks a capability

Do not give up an agent because one endpoint is missing. Downgrade its tier instead. The
reference platform itself forced three downgrades, and they are the model for yours:

- **There is no rate or availability write API**, so A5 is advise-only. It computes
  variance and trends in Code Interpreter and says what it would change; a person
  changes it.
- **There is no room-status write endpoint**, so no agent can take a room out of order.
  A2 reports a room that looks stuck; it does not unstick it.
- **Starting a night audit is administrator-only**, and no agent is an administrator, so
  A4 reports readiness and a person starts the run.

To downgrade: remove the write tool from that target's schema and handler, drop it from
the prompt's tool list, and say in the prompt what the agent should recommend instead.
Keeping a write tool that always returns "not supported" is worse than removing it — the
model will keep trying, and every try is a tool call you pay for.

Tell the agents what your platform does *not* do, too. The reference prompts state that
nothing sets `NO_SHOW` and that no expected-arrival time exists anywhere in the data,
because a model that is not told will assume both. Your platform will have its own
equivalents; the list in your prompts is part of the adapter.

---

## 7. Mixing a CRS and a PMS from different vendors

The reference has two APIs under one identity provider, and the client already treats
them separately: every call names its `surface`, `"crs"` or `"pms"`. Two vendors
therefore means two base URLs and, usually, **two credentials per agent** — one per
system — held side by side in the agent's secret.

Two problems appear that one vendor hides:

- **The same reservation has two identifiers.** The CRS knows a confirmation number; the
  PMS knows a stay or folio id. Keep the mapping in the adapter that needs it (usually
  arrivals and billing) and put both identifiers in the tool's response, so the agent can
  cite either and a person can find the record in either system.
- **The two systems disagree.** A booking modified in the CRS may not have reached the
  PMS yet. Tell the model which system is authoritative for which question — the PMS for
  anything about a guest in house, the CRS for anything about a future stay — and have
  the adapter say which system each fact came from.

---

## 8. A worked sequence: replacing the PMS

A concrete order that keeps every step verifiable. It assumes the CRS stays as it is.

1. **Map the contract.** For each PMS-surface tool in `schemas/*.json`, write down the
   endpoint in your PMS that answers it, the translation it needs, and its tier. Mark each
   gap and decide its downgrade ([§6](#6-when-your-platform-lacks-a-capability)) now,
   not halfway through.
2. **Decide the identity model** ([§5](#5-identity-the-hard-part)). If you end up in 5.2,
   write the property allowlist and the per-target operation list before any handler.
3. **Replace discovery** (seam 1) with a JSON config for the new base URL.
4. **Replace transport** (seam 2). Unit-test it offline first: token caching, the single
   `401` refresh, pacing, and verbatim error envelopes. `tests/unit/test_foundation_client.py`
   has the cases to keep.
5. **Rewrite one adapter end to end — start with night audit.** It is read-only, so a
   mistake costs nothing, and it exercises folios, stays and daily reporting. Deploy only
   the tools stack and check it with `tests/integration/verify_tools.py`, which invokes the
   Lambdas directly with no agent involved.
6. **Put the Gateway in front** and run `tests/integration/verify_gateway.py --no-model`.
   It proves tool names, schemas and interceptors without spending a model call.
7. **Run the advisory agents** — `tests/integration/exercise_agents.py --only a4,a5` — and
   read the answers. They write nothing, and they are where translation mistakes show up
   as confident wrong numbers.
8. **Then the Tier-1 writers** (A1, A2) against a test property, then A3's proposals, and
   run `tests/integration/verify_console.py` last. It checks that a Tier-2 call without a
   bound approval is refused; that check must pass before anything else ships.
9. **Arm triggers one at a time** ([seam 4](#seam-4--events--infrastacksorchestration_stackpy)),
   advisory agents first.

---

## 9. What not to change

When the adapter gets awkward, these are the tempting shortcuts. Each has a reason not
to take it.

| Tempting | Why not |
|---|---|
| Give every agent one full-access credential | Collapses per-agent scoping into prompt wording. See [§5.2](#52-when-the-platform-cannot-scope-for-you) for what to do when the platform forces it |
| Read the database directly because the API is slow | Skips the platform's authorization, validation and audit trail |
| Rename tools to match the vendor's vocabulary | The prompts and evaluators name them; the contract is what makes the rest portable |
| Loosen the approval binding to fit a different folio key | Bind to the new key instead. Binding to the action alone was a real vulnerability once |
| Paraphrase vendor errors into friendly messages | The model reasons on the real error |
| Arm the schedules as part of the deploy | Agents writing to a live system on a timer is an operator's decision |
| Keep a write tool that always returns "unsupported" | The model keeps calling it. Remove it and downgrade the tier |
