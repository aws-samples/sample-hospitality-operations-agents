# Demo Guide

Seven demos, in the order I would run them. Each is self-contained; each states what
to say, what to click, what to point at, and what might go differently — because
these are live agents against live data and they do not repeat themselves exactly.

**The strongest two, if you only have ten minutes:** Demo 1 (a measurably better
decision) and Demo 3 (a human is genuinely required to move money). One shows the
value, the other shows why it can be trusted with the value.

---

## Before you start

### Sign in

Open the console (`ConsoleUrl` from the `hotel-ops-agent-frontend` stack) and sign in
with a staff account from **the platform's own seed data** — this project creates no
console users. Use a chain-level `Admin` or `Manager` account; the platform's
`DEMO_GUIDE.md` lists them.

Property-scoped accounts (FrontDesk, Housekeeping, a single-property GM) are pinned to
their own property by `custom:property_id` and will show an **empty console** unless
that property is in your `pilotPropertyIds`. That is correct behaviour and worth
showing on purpose in Demo 6 — just don't discover it live in Demo 1.

Put your pilot property's id in the header field. Leave it blank only for A5.

### The three panes

| Pane | What it is |
|---|---|
| **Copilot** | Ask the orchestrator anything. It routes to a specialist and shows you which one, with the tool calls appearing as they happen |
| **Approvals** | The only place in the system where money movement is released |
| **Run history** | Every run, its full trajectory, and a place to record whether the agent was right |

### Set expectations in one sentence

> "Runs take one to four minutes, because a real agent is reading real inventory and
> deciding. You'll see each tool call appear as it makes it."

Do not apologise for the latency — the tool-call trail arriving live *is* the
interesting part. It is the audit log, being written in front of you.

### Have a terminal open

Two demos are much stronger with a terminal alongside the browser: Demo 1 (to show
the comparison) and Demo 3 (to show the gate refusing with no model in the path).

---

## Demo 1 · A room assignment that is measurably better

**~4 minutes. The headline.**

### The pitch

> "The platform already assigns rooms. It does it with one query: highest floor,
> lowest room number, first match. It ignores every attribute of the guest and every
> attribute of the room. Let's give an agent the same data and compare."

### Do this

In **Copilot**, with your pilot property set:

```
Pre-assign rooms for the unassigned arrivals at this property.
```

While it runs, narrate what appears: `arrivals_agent` picked up, then
`list_arrivals`, then `list_rooms`, then a series of `assign_room` calls — each one
tagged **"wrote assign_room"** in amber, because the interceptor only records an
action when the write actually succeeded.

### Point at

The answer names each guest, the room, and **why**. Then show the comparison:

```bash
tests/integration/exercise_agents.py --only a1
```

Observed on real seed data, same 14 arrivals, same inventory:

| | Agent | The platform's `ORDER BY floor DESC, room_number ASC LIMIT 1` |
|---|---|---|
| Room type matches what was booked | **7 of 7 placed** | **1 of 14** |
| Accessibility-reserved rooms given to guests with no such need | **0** | **2** |

### The part worth dwelling on

On one run the agent placed 7 of 14 arrivals and **refused the other 7**:

> *"Standard Double Queen is oversold: 11 arrivals booked that type, but only 4 rooms
> of it were free. After the 4 went to the three Diamonds and the Gold guest closest
> to Diamond, these seven have no matching room and were not auto-downgraded, since
> that's a rate/comp decision outside A1's authority."*

> "That refusal is the more valuable half. It ranked by loyalty tier, gave the scarce
> rooms to the guests who had earned them, flagged ten Deluxe rooms as an upgrade
> path, and stopped — because downgrading someone's booking is a commercial decision
> a human owns. The naive query places all fourteen, most of them wrongly, and tells
> you nothing."

### If it goes differently

- **"All arrivals already have rooms."** A previous run assigned them. Ask for a wider
  window: *"Look ahead five days and pre-assign anything still unassigned."*
- **It places everyone.** Fine — the type-match comparison still lands. The refusal
  depends on inventory being tight that day.

---

## Demo 2 · An agent that says what it cannot know

**~2 minutes. Best told immediately after Demo 1.**

### The pitch

> "The interesting constraint isn't what the agent can do. It's what it refuses to
> claim."

### Do this

In **Copilot**:

```
Assign room 1204 to the next arrival because the guest prefers a high floor away from the elevator.
```

### Point at

It will tell you the platform exposes **no guest preference data** to staff
credentials — the endpoints that would carry it are guest-owner-only and return 403 —
and ask you to supply the preference if you have it out of band.

> "Every endpoint that would return preferences was checked. The agent's prompt says
> so, and says never to claim a preference match in the reason it writes to the audit
> log, because a human reads that reason when judging whether the agent chose well. A
> fabricated justification is worse than a mediocre room."

Then open **Run history**, click the run, and show the `reason` recorded on the
assignment. It cites loyalty tier, bed configuration, occupancy fit, floor — and
never a preference.

---

## Demo 3 · The approval loop: a human is genuinely required

**~6 minutes. The safety story, and the one to run if someone asks "would you let
this touch billing?"**

### The pitch

> "Three tools move money. An agent may reorganise work all day and must never move
> money on its own. Let me show you that's mechanical, not a promise in a prompt."

### Part A — the gate, with no model anywhere

In a terminal:

```bash
tests/integration/verify_console.py
```

Talk over it. The checks that matter:

```
PASS  a non-approver cannot release it                      (403 NOT_AN_APPROVER)
PASS  the person who filed it cannot release it themselves   (403 SELF_APPROVAL)
PASS  even the real token opens nothing when it arrives as an
      argument, which is the only channel a model has          (APPROVAL_REQUIRED)
PASS  the token cannot be spent on a folio the approval did not name   (APPROVAL_MISMATCH)
PASS  nor for an amount the approval did not name                      (APPROVAL_MISMATCH)
PASS  and exactly as approved it reaches the foundation, which rejects
      it on its own terms — so the gate opens, and nothing moved        (NOT_FOUND)
```

> "That last one is the important pair. The gate *opens* for a valid approval — it's
> a working guardrail, not a wall — and the same token, seconds later, is refused for
> a different folio and a different amount. An approval authorises one action, one
> target, one amount. And there is no model in this test at all: the refusal happens
> in a Gateway interceptor that runs before the tool Lambda. A prompt injection can
> talk a model into *attempting* a charge; it cannot talk that interceptor into
> allowing one."

### Part B — the human in the loop

This needs **two people**, because the console refuses to let anyone release a
proposal they filed. Use two browsers (or a private window): one signed in as the
person filing, one as a Manager or Admin who releases it.

In the console, **Approvals** → file a proposal as the first person (or turn one A3
produced into a proposal), then, as the second:

1. Show the pending row: action, folio, amount, and the agent's stated reason.
2. Try **Approve** without a note → refused. *"The agent's reason records what it
   wanted. The note records that a named human agreed. That's the only part of the
   chain carrying authority."*
3. Add a note and approve. The console tells you the agent has been re-invoked and
   gives you the run id.
4. Follow the link into **Run history** and watch the execution run.

### Point at

- The approval token is **never shown** anywhere in the UI, because the API never
  returns it. And the agent never sees it either: it travels with the execution run
  as a Gateway header, and the Gateway attaches it to the one call it authorises.
  Nothing to copy, nothing to paste into a chat, and nothing for a model to repeat.
- Try approving from the same account that filed the proposal: *"Moving money needs
  two people: the one who asks and the one who agrees."*
- The token expires in 15 minutes whether used or not.

### If it goes differently

A3 sometimes **refuses** an approved charge, and its reasons are good. Observed:

> *"The approval is attributed to an agent identity, not a human operator in the ops
> console. And the folio ID is all-zeros and doesn't correspond to any folio it has
> actually read. It won't post against a target it hasn't verified line-by-line."*

**Lean into it.** *"It just refused an approval because it couldn't verify the
approver was human and couldn't verify the folio existed. It read before writing —
and that's in its instructions, not something I asked for here."*

---

## Demo 4 · Night audit readiness — finding real exceptions

**~3 minutes. The most immediately relatable to anyone who has worked a front desk.**

### The pitch

> "Every hotel closes the books at night. Before that happens, someone walks the
> exceptions. This does it in three minutes and never forgets a category."

### Do this

```
Are we ready for tonight's audit?
```

### Point at

A verdict, then the four categories, each with names and numbers. Observed:

> **READY with caveats.** Two guests — Katherine Allen (rm 1004) and Amy Le (rm 1009)
> — are due out today, still `CHECKED_IN`, each with **$474.30 outstanding and zero
> payments on file**. No rooms stuck mid-state. No stored audit run yet, which is the
> expected pre-audit state.

Then the detail worth showing:

> "It also flagged that the daily report says 32 rooms occupied and 30 rooms sold, so
> the two ADR figures derived from them disagree. That's a known inconsistency in the
> platform — two endpoints computing the same metric from different denominators — and
> the agent is instructed to surface it rather than pick the tidier number. Picking
> the tidier number is how a reporting bug survives for a year."

Note it also cannot trigger the audit itself. A4 is advise-only by design: the
platform's audit endpoint is `Admin`-only, and no agent here gets `Admin`.

---

## Demo 5 · Regional performance, and arithmetic done in code

**~5 minutes. The longest run — start it, then talk while it works.**

### The pitch

> "Cross-property analysis is where language models are most confidently wrong. Ask
> one to compute variance over 92 days of JSON in its head and it will give you a
> number that looks right."

### Do this

**Clear the property field** — this is the only chain-wide agent — and ask:

```
How did the portfolio do yesterday? Which properties moved most against their trend?
```

### Point at

The answer ranks properties by **RevPAR dollar impact rather than percentage**:

> *"A more meaningful cut than raw %, since a small property swinging 80% off a
> near-empty base isn't operationally comparable to a flagship losing $12K."*

Then show that the arithmetic was executed, not estimated:

```bash
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/<runtime-id>-production \
  --filter-pattern '"code interpreter session="' --query 'events[-1].message'
```

> "That's a sandboxed Code Interpreter with no network access. The agent fetched the
> data through the tool plane, embedded it in Python, and ran the trend fit there.
> Only A5 has it — the other four don't reason over a series, so they don't get it."

### The bug it found

> "On its second-ever run it reported that the platform's occupancy endpoint ignores
> its own `startDate` — three different historical windows return byte-identical data.
> We checked the handler. It's right: `startDate` is accepted and used by no query.
> The tool now attaches a note explaining what the numbers actually mean, so no agent
> presents a single-day snapshot as a trend."

### If it goes differently

A5's cost varies run to run — 13 to 36 tool calls for the same question. If it takes a
long time, that's the honest state, and it's exactly what Demo 7's trajectory graders
exist to measure.

---

## Demo 6 · Housekeeping, and the authorization model

**~3 minutes. Short, and it makes the security model concrete.**

### Do this

```
Sequence and assign the open housekeeping tasks at this property.
```

### Point at

If the board is clear it will say so plainly — *"all 459 existing tasks are already
INSPECTED and closed out; zero rooms in any active workflow state"* — rather than
inventing work. That is worth showing: an agent that reports nothing to do is
behaving correctly.

### Then show the boundary

Sign out and sign in as the **housekeeping** staff account. In **Approvals**:

- The queue is readable — seeing what's pending isn't privileged.
- The **Approve button is gone**, and the header says your account can't release
  anything.

> "That's enforced in three independent places. The console hides the button. The API
> re-checks the group and returns 403. And the housekeeping *agent* signs in to the
> platform as a Cognito user in the `Housekeeping` group, so the platform's own
> authorizer rejects a billing write regardless of what any of our code does. The
> agent can't post a charge even if something convinces it to try."

Also try reading another property's runs as this account → `403 OUT_OF_SCOPE`. It
refuses rather than silently showing you your own property, which would be worse.

---

## Demo 7 · Grading the agents

**~3 minutes. For the audience that asks "how do you know it's any good?"**

### The pitch

> "Traces tell you what happened. They can't tell you whether the agent was right.
> That answer arrives days later, when a person either lets a decision stand or
> reverses it."

### Do this

```bash
tests/integration/verify_evaluation.py --no-wait --since-minutes 180
```

### Point at

Six graders scoring live traffic — four built-in trajectory graders plus two custom
judges. Then read out an actual judgment from the honesty judge:

> *"list_folios (all 552, paginated fully) returned totalAmount:null everywhere.
> compare_metrics returned 404 with live figures matching exactly what's quoted
> (occupied 32, roomsSold 30, revenue 7521.24, ADR 250.71) … billing_agent then pulled
> folio detail for both guests, confirming $237.15 × 2 = $474.30 each, zero payments —
> this matches the final answer's numbers exactly."*

> "That's a second model reconciling every number in the answer against the tool
> results that produced it. The failure it's hunting is an agent that says 'I assigned
> room 412' when it didn't — a confident answer an operator acts on without reading
> the trajectory. That's the highest-cost, lowest-visibility failure this system can
> produce."

### Close the loop

In **Run history**, open a run and use **It was right** / **I overrode it** with a
reason.

> "That verdict is the ground truth the judges are ultimately measured against. If
> operators overturn 30% of the room assignments, the agent is bad no matter what the
> judge scored it. Where the judge and the humans disagree is the signal that tells
> you the rubric is wrong — and that's what makes the rubric improvable instead of
> decorative."

### Be honest about the limitation

The room-quality judge currently also grades unrelated tool calls, and says so itself:
*"the tool call under judgement is not a room-assignment decision at all."* A
`TOOL_CALL`-level evaluator runs on every call and there's no per-evaluator filter. It
has its own filtered config now, but the filter key is unverified. The verifier
reports the count of inapplicable gradings on every run — *"which is the difference
between a limitation and a bug you don't know about."*

---

## Demo 8 · Unattended operation (optional)

**~2 minutes. Only if the audience cares about the operational picture.**

Everything ships **disabled** — four schedules and three event rules, all `DISABLED`.

```bash
aws scheduler list-schedules --group-name hotel-ops-agent \
  --query 'Schedules[].[Name,State]' --output table
```

> "Arming these is ~120 agent runs per property per day writing to a live database.
> That's a decision about money and about someone else's system, so it's a switch a
> human throws, not a side effect of a deploy."

If they are armed, show the invoker draining the queue:

```bash
aws logs tail /aws/lambda/hotel-ops-agent-invoker --follow
```

And the reactive path: the rules subscribe to the platform's **existing** event bus —
`reservation.created` wakes A1, `checkinout.checked_out` wakes both A2 (turnover) and
A3 (folio integrity). Adding a rule to a bus doesn't modify the platform's stack,
which is why it's one of only two things this project adds to it.

---

## Questions you will get

**"What stops it doing something catastrophic?"**
Three tiers. Tier 1 (room assignment, housekeeping) auto-executes and is trivially
reversible by a human at the desk. Tier 2 (anything touching money) is refused by a
Gateway interceptor outside the model's reach. Tier 3 (audit, regional) has no write
tools at all. Demo 3 shows the middle one mechanically.

**"Could a prompt injection make it post a charge?"**
It could make the model *try*. The refusal happens in a Lambda that runs before the
tool Lambda, and the model has no channel to it other than the tool call itself. The
billing Lambda then checks again independently, so neither one being misconfigured
opens the gate.

**"How does it not break the hotel platform?"**
It never touches the database and never bypasses the API. Every call goes through one
Gateway, and each of the five tool Lambdas signs in as its own Cognito user, so the
platform's own authorizer enforces per-agent scope. A non-interference check runs
before and after every deploy and asserts the platform's stack status, timestamp, and
group count are unchanged. The only things this project adds are Cognito users and
event-bus rules.

**"What does a run cost?"**
5k–20k tokens for most, more for a portfolio review. The cost driver is how many runs
you allow, which is why the schedules ship disabled and scoped to one pilot property.

**"Why five agents instead of one?"**
Identity. Each agent signs in as a different Cognito user in a different group, so the
permission model is enforced by the platform rather than by prompt wording. One agent
would need one identity with the union of all permissions.

**"Did it actually find anything?"**
Three defects nobody had reported: a live overcharge on a settled folio (five
room-nights billed for a four-night stay), a reporting endpoint that ignores its own
date range, and an event rule routing an event nothing publishes. Demos 4 and 5 show
two of them.

---

## Resetting between demos

Nothing needs resetting, but two things drift:

- **A1 runs out of work** once it has assigned everything in its window. Widen the
  window in your prompt, or use a different property.
- **The approval queue empties** as tokens expire on their TTL. File a fresh proposal
  before Demo 3 if the pane is empty.

```bash
# What's pending right now
aws dynamodb scan --table-name hotel-ops-agent-approvals \
  --filter-expression '#s = :p' \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":p":{"S":"PENDING"}}' \
  --query 'Items[].[proposalId.S,action.S,amount.N]' --output table

# The last few runs, newest first
aws dynamodb query --table-name hotel-ops-agent-decisions \
  --index-name property_id-ts-index \
  --key-condition-expression 'property_id = :p' \
  --expression-attribute-values '{":p":{"S":"<your-property-id>"}}' \
  --no-scan-index-forward --max-items 5 \
  --query 'Items[].[ts.S,agent.S,tool.S]' --output table
```
