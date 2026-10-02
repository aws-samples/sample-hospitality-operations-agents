# Hotel Operations Agent — Development Specification

**Built on:** the AnyCompany Hospitality Platform ([aws-samples/sample-hospitality-systems](https://github.com/aws-samples/sample-hospitality-systems))
**Scope:** a single orchestrator agent — the **Hotel Operations Agent** — coordinating five specialist sub-agents that streamline front desk, housekeeping, night audit, billing accuracy, and multi-property oversight
**Hard constraint:** **no changes to the existing platform.** Every capability below is reachable through APIs, events, and data that exist today.
**Status:** this is the base document for development. Technology choices for the agent runtime are deliberately out of scope.
**Date:** 2026-09-03

---

## 1. Purpose

The hospitality platform already owns the hard part: authenticated, tenant-scoped, role-guarded write APIs over a real PMS data model, an event bus that narrates every operational state change, and an analytics lake that makes history queryable in SQL.

What it has no notion of is judgment. Every decision it makes is a fixed rule — the room assigner takes the highest-numbered floor, the housekeeping queue is ordered by priority and age, the night audit posts what it can derive and silently skips what it can't. Those rules are correct and fast and completely blind to context.

The Hotel Operations Agent supplies that judgment. It is a reasoning layer, not a re-platforming. Five sub-agents, each owning one operational domain, coordinated by one orchestrator that holds the shared picture of a property and resolves conflicts between them.

**Why an orchestrator rather than five independent agents:** the five domains are not independent. A room assignment determines a housekeeping priority. A housekeeping completion forecast constrains which rooms can be assigned. A night-audit exception on a folio is the same finding the folio agent surfaces at checkout. Five agents acting alone would each be locally correct and collectively incoherent — reassigning the same room, double-proposing the same adjustment, contradicting each other in the same morning brief. One orchestrator that owns sequencing, shared state, and conflict resolution eliminates that class of failure by construction, and gives a single place to add the sixth, seventh, and eighth capability later.

---

## 2. Design Principles

1. **No platform changes.** No new endpoints, no schema migrations, no changes to existing handlers. If a capability needs one, it is out of scope for this document (see §11).
2. **The platform owns the rules; the agent owns the judgment.** Business logic is never duplicated into the agent layer — the agent proposes, the platform validates. When the platform rejects an action, that rejection is authoritative information, not an error to route around.
3. **The agent is a staff member.** It authenticates as a Cognito service user in a specific group with specific property or region claims. Its permissions are exactly the permissions of the role it holds.
4. **Every write is attributable.** Provenance fields already exist throughout the schema. Populated with the agent principal, the audit trail is automatic and indistinguishable in quality from a human's.
5. **Reorganize freely; never move money alone.** The agent may re-sequence work and reassign rooms without a gate. Charges, voids, and loyalty adjustments always require human approval.
6. **Advisory beats wrong.** Where no write path exists, the agent produces a ranked recommendation and stops. A confidently wrong action is far more expensive than a well-evidenced suggestion.
7. **Extensible by contract, not by refactor.** Sub-agents conform to a fixed interface (§5). Adding one is a registration, not a redesign.

---

## 3. Architecture

### 3.1 The shape

```
                        ┌─────────────────────────────────┐
                        │   HOTEL OPERATIONS AGENT        │
                        │   (orchestrator)                │
                        │                                 │
                        │  • owns property context        │
                        │  • routes requests & triggers   │
                        │  • sequences dependent work     │
                        │  • resolves cross-domain        │
                        │    conflicts                    │
                        │  • assembles unified output     │
                        │  • enforces approval tiers      │
                        └───────────────┬─────────────────┘
                                        │
      ┌───────────────┬─────────────────┼─────────────────┬───────────────┐
      │               │                 │                 │               │
┌─────▼──────┐ ┌──────▼──────┐ ┌────────▼───────┐ ┌───────▼──────┐ ┌──────▼───────┐
│ HOUSEKEEP- │ │ NIGHT AUDIT │ │   ARRIVALS &   │ │  PORTFOLIO   │ │    FOLIO     │
│    ING     │ │  EXCEPTION  │ │  FRONT-DESK    │ │   INSIGHT    │ │   ACCURACY   │
│  DISPATCH  │ │             │ │   READINESS    │ │              │ │              │
│    (A1)    │ │    (A2)     │ │      (A3)      │ │     (A4)     │ │     (A5)     │
└─────┬──────┘ └──────┬──────┘ └────────┬───────┘ └───────┬──────┘ └──────┬───────┘
      │               │                 │                 │               │
      └───────────────┴─────────────────┼─────────────────┴───────────────┘
                                        │
                        ┌───────────────▼─────────────────┐
                        │      TOOL LAYER                 │
                        │  thin wrappers over existing     │
                        │  CRS + PMS REST endpoints,       │
                        │  event bus, analytics queries    │
                        └───────────────┬─────────────────┘
                                        │
                        ┌───────────────▼─────────────────┐
                        │   EXISTING PLATFORM (unchanged)  │
                        │   CRS API · PMS API · Cognito ·  │
                        │   event bus · workflows ·        │
                        │   Aurora · analytics lake        │
                        └─────────────────────────────────┘
```

### 3.2 What the orchestrator owns

**Property context assembly.** Before dispatching, the orchestrator builds the shared snapshot every sub-agent needs: the room board, today's arrivals and departures, in-house stays, the open task queue, and headline metrics. Assembling this once and passing it down avoids five sub-agents independently hitting the same endpoints, and — more importantly — guarantees they reason over the *same* picture. Two sub-agents working from snapshots taken ninety seconds apart is a conflict generator.

**Routing.** Three kinds of input arrive:
- *Scheduled cadence* — housekeeping re-sequencing through the turn window, the arrivals sweep, the post-night-audit review, the morning portfolio brief.
- *Platform events* — a checkout, a new same-day reservation, a payment failure. These map to specific sub-agents.
- *Human requests* — a manager or front-desk user asking a question or asking for a specific action. The orchestrator decides which sub-agents contribute.

**Sequencing of dependent work.** Some pairs have a strict order. Arrivals readiness (A3) must run before housekeeping dispatch (A1) in the evening sweep, because tomorrow's room assignments determine tomorrow's cleaning priorities. Night audit exceptions (A2) must complete before the portfolio brief (A4), because the brief aggregates them. The orchestrator holds this dependency graph — no sub-agent needs to know about another.

**Conflict resolution.** See §3.4.

**Unified output.** A property manager should receive one morning brief, not five. The orchestrator merges sub-agent findings, de-duplicates overlapping items (A2 and A5 will both find some folio problems), and ranks by impact across domains.

**Approval enforcement.** Approval tiers (§7) live in the orchestrator, not in each sub-agent. A sub-agent returns a proposed action; the orchestrator decides whether it executes immediately, queues for human approval, or is advisory only. This is deliberate — it means a new sub-agent cannot accidentally grant itself write authority.

### 3.3 What a sub-agent owns

A sub-agent is narrow by design: one operational domain, one set of tools, one clear output contract.

| Concern | Owned by |
|---|---|
| Domain reasoning | Sub-agent |
| Which tools it may call | Sub-agent definition, enforced by the orchestrator |
| Whether a proposed write executes | Orchestrator |
| Cross-domain awareness | Orchestrator |
| Identity / credentials | Orchestrator (per-sub-agent identity, §6) |
| Output formatting for humans | Orchestrator |

Sub-agents do not call each other. All coordination goes through the orchestrator. This keeps the dependency graph explicit and adding a sixth sub-agent from becoming an N² integration problem.

### 3.4 Conflict resolution rules

The specific cross-domain conflicts that will arise, and how the orchestrator settles them:

| Conflict | Resolution |
|---|---|
| A3 wants to assign a room that A1 has not yet scheduled for cleaning | A3's assignment is provisional until A1 confirms a feasible turn time. If infeasible, A3 picks another room. |
| A1's sequencing would deprioritize a room A3 just assigned to a high-tier arrival | A3's assignment wins; A1 re-sequences around it. Guest commitment outranks route efficiency. |
| A2 and A5 both flag the same folio discrepancy | De-duplicated on `(folio_id, charge_date, exception_type)`. One proposal reaches the human. |
| A4's brief contradicts a property-level finding from A2 | A2 is authoritative for its own property; A4 reports the aggregate and cites A2's detail. |
| Two sub-agents want to write to the same record in one cycle | Orchestrator serializes and re-reads between writes. Never concurrent writes to one entity. |
| A sub-agent's write returns a state-conflict rejection from the platform | Authoritative — a human or another process moved it. Re-read; do not retry. |

### 3.5 Extensibility

Adding a sub-agent requires four things and no changes to the existing five:

1. **A domain boundary** — what it reasons about, stated narrowly enough that its outputs don't overlap an existing sub-agent's.
2. **A tool allowlist** — the specific existing endpoints it may read and write.
3. **An identity** — a Cognito service user in the least-privileged group that can do its job.
4. **A registration** with the orchestrator: its trigger conditions, its dependencies on other sub-agents, its approval tier, and its output schema.

Candidate future sub-agents that fit this contract without platform changes are listed in §12.

---

## 4. The Boundary — Platform Capabilities Available Today

Everything the five sub-agents do is a composition of the assets below.

### 4.1 APIs and identity

- **CRS API** — properties, availability, reservations, guests, payments, booking. Some endpoints public.
- **PMS API** — stays, housekeeping, billing, loyalty, night audit, reporting. All authenticated.
- **Cognito** — one user pool for guests and staff. Six staff groups: `Admin`, `Manager`, `FrontDesk`, `Housekeeping`, `RegionalManager`, `RevenueManager`. Custom claims `custom:property_id`, `custom:region`, `custom:guest_id`. Machine-to-machine resource server with `read`/`write`/`admin` scopes.
- **Uniform response envelope** — `{"success": true, "data": {...}, "metadata": {...}}` or `{"success": false, "error": {"code","message","details"}}`. This matters operationally: it lets an agent cleanly distinguish a business rejection (`INVALID_STATE`, `409 ALREADY_ASSIGNED`) from a transport failure, and reason about the difference.
- **Tenant scoping** is enforced centrally in `src/layers/common/utils/tenant.py` — `require_groups`, `verify_property_access`, `get_accessible_properties`. An agent's group and claims *are* its permission envelope, with no new authorization code.

### 4.2 The write surface

| Endpoint | Groups allowed | Effect |
|---|---|---|
| `POST /stays/{reservationId}/checkin` | FrontDesk, Manager, Admin | Reservation → `CHECKED_IN`, room → `OCCUPIED`, check-in record written, workflow token released |
| `POST /stays/{reservationId}/checkout` | FrontDesk, Manager, Admin | Reservation → `CHECKED_OUT`, triggers billing + housekeeping chain |
| `PUT /stays/{reservationId}/room` | FrontDesk, Manager, Admin | Room move for an in-house stay |
| `PUT /stays/{reservationId}/assign-room` | **Manager, Admin** | Pre-arrival assignment on a `CONFIRMED` reservation |
| `PUT /housekeeping/tasks/{taskId}/assign` | Housekeeping, Manager, Admin | Task → `ASSIGNED`, sets `assigned_to` (≤100 chars) |
| `POST /housekeeping/tasks/{taskId}/complete` | Housekeeping, Manager, Admin | Task → `COMPLETED`, releases cleaning token |
| `POST /housekeeping/tasks/{taskId}/inspect` | Housekeeping, Manager, Admin | Task → `INSPECTED`, room becomes sellable |
| `POST /billing/folios/{folioId}/charges` | **Manager, Admin** | Post `SERVICE` or `ADJUSTMENT` — `OPEN` folios only, non-zero amount, description ≤500 chars |
| `POST /billing/folios/{folioId}/void` | **Manager, Admin** | Void a charge |
| `POST /loyalty/{guestId}/adjust` | **Manager, Admin** | Points adjustment — `reason` mandatory, negative resulting balance rejected |
| `POST /loyalty/{guestId}/redeem` | FrontDesk, Manager, Admin | Redeem points (10,000 = 1 free night) |
| `POST /audit/runs` | **Admin only** | Trigger a night audit run |

Note the pattern: **every consequential write is already Manager- or Admin-gated.** The natural design — `Manager` for advisory sub-agents, `Admin` for nothing — inherits a sane blast radius for free.

### 4.3 The read surface

- `GET /stays?propertyId=&status=&date=&page=&limit=` — arrivals and in-house board. Returns `guestName`, `loyaltyTier`, `roomType`, `roomNumber`, `status`, dates, `checkedInAt`/`checkedOutAt`. Default status filter `CONFIRMED, CHECKED_IN`; limit capped at 100.
- `GET /housekeeping/rooms/summary` — the richest single operational read: `totalRooms`, `occupancyPercent`, counts for available/occupied/dirty/cleaning/inspecting/outOfOrder, **plus a per-room list** of `{roomId, roomNumber, floor, roomType, status}`. Single property only.
- `GET /housekeeping/tasks`, `GET /housekeeping/tasks/{taskId}` — queue with type, priority, status, `assigned_to`.
- `GET /billing/folios`, `GET /billing/folios/{folioId}` — folio with charge lines.
- `GET /loyalty/{guestId}`, `GET /loyalty/{guestId}/transactions` — tier, balance, ledger.
- `GET /reporting/{propertyId}/daily` — reservations created, check-ins, check-outs, occupancy, room revenue, `roomsSold`, tax posted, payments, ADR, housekeeping tasks completed.
- `GET /reporting/occupancy` — per-property occupancy, revenue today, check-ins/outs today, chain summary. Scope precedence: caller property → caller region → `?region=`.
- `GET /reporting/range` — up to **92 days**, per-day breakdown plus totals, `_all` sentinel for chain-wide. Property-pinned callers cannot widen scope.
- `GET /audit/reports/{propertyId}` — historical night audit metrics.
- `GET /guests`, `GET /guests/{guestId}`, `GET /properties`, `GET /properties/{propertyId}/room-types` — directory and profile reads.

### 4.4 The data model

Fields that specifically enable agent reasoning, and that a generic PMS integration would not have:

- **`rooms`** — `floor`, `wing`, `is_connecting`, `connecting_room_id`, `features TEXT[]`, `last_cleaned_at`; status in `CLEAN/DIRTY/INSPECTED/OUT_OF_ORDER/OUT_OF_INVENTORY`. Floor, wing, and features are what make route optimization and preference matching real rather than hand-wavy.
- **`room_types`** — `accessibility_type`, `smoking_allowed`, `max_occupancy`, `bed_configuration`, `amenities TEXT[]`, `base_rate`.
- **`guests`** — `vip_level`, `preferences JSONB`, `comm_prefs JSONB`, `tags TEXT[]`, `total_stays`, `total_spend`, `last_stay_date`, `language`, `loyalty_tier`, `points_balance`.
- **`reservations`** — request flags `early_check_in_requested`, `late_check_in_requested`, `early_check_out_requested`, `late_check_out_requested`, `self_check_out_requested`; `adults`/`children`; `additional_notes`; `attributes JSONB`; and **rate snapshot fields** (`amount_per_night`, `total_before_tax`, `total_after_tax`, `refundable`, `guarantee_type`, booked rate-plan and room-type codes). The snapshot is what makes billing reconciliation definitive instead of approximate.
- **`housekeeping_tasks`** — `task_type` in `CHECKOUT/PRE_ARRIVAL/MAINTENANCE`, `priority` in `HIGH/NORMAL/LOW`, status machine `PENDING→ASSIGNED→CLEANING→COMPLETED→INSPECTING→INSPECTED` plus `FAILED`, `assigned_to VARCHAR(100)` free text.
- **`folios`** / **`charges`** / **`payments`** — folio status `OPEN/PENDING_PAYMENT/PAID/VOID/PAYMENT_FAILED`; charge types `ROOM_RATE/TAX/SERVICE/ADJUSTMENT` with `charge_date` and `status ACTIVE/VOIDED`, `voided_by`.
- **`night_audit_runs`** — `metrics JSONB` with `UNIQUE (audit_date, property_id)`. A ready-made time series for trend reasoning.
- **`availability`** — `available` is a generated column (`total_inventory - sold - blocked`), plus `overbooking_allowance`. Readable truth for oversell detection.
- **`checkinout_records`** — `record_type`, `performed_by`, `recorded_at`, `notes`. Stay history and provenance.
- **Loyalty economics** — tier thresholds on `total_stays` (`SILVER` 5, `GOLD` 10, `DIAMOND` 20), point multipliers (1.0 / 1.25 / 1.5 / 2.0), 10,000 points per free night.

### 4.5 Events, workflows, and history

- **Custom event bus with archive.** 17 implemented events, notably `reservation.created/modified/cancelled`, `checkinout.checked_in/checked_out`, `billing.payment_processed/payment_failed`, `housekeeping.room_ready`, `audit.night_completed`, `loyalty.tier_changed`, `guest.created/updated`, `payment.captured/refunded`.
- **Queue consumers with dead-letter queues and alarms** for housekeeping, billing, loyalty, and notifications, with partial-batch failure reporting.
- **Workflows with task tokens** — checkout billing and housekeeping dispatch. A stay or a room turn is a *suspended workflow waiting on a token*, which is exactly the shape an agent can advance.
- **Analytics lake** — every event lands in S3 as Parquet and is exposed as a Glue table `events` (`event_id, source, detail_type, event_time, region, account, detail`), partitioned by source/year/month/day with partition projection, queryable via a dedicated Athena workgroup. **Historical operational questions are answerable in SQL with no new pipeline.** Turn times, cancellation timing, payment-failure clustering all come from here.
- **Outbound email** on four event types. Outbound only.

### 4.6 Two known-naive implementations that are the clearest opportunities

**Room auto-assignment at check-in** (`src/pms/checkinout/check_in.py`) selects with:

```sql
SELECT room_id, room_number, room_type_id, status, floor FROM rooms
WHERE property_id = %s AND room_type_id = %s AND status = 'AVAILABLE'
ORDER BY floor DESC NULLS LAST, room_number ASC LIMIT 1
```

Highest floor, lowest room number. No guest preference, no tier, no accessibility, no connecting-room logic for families, no spread across housekeeping sections. Every field needed to do better already exists. A3 replaces this heuristic **without touching this file**, because `PUT /stays/{reservationId}/assign-room` already provides the pre-arrival write path.

**Pre-arrival task creation** (`src/pms/housekeeping/process_event.py`) creates a `PRE_ARRIVAL` housekeeping task only when a room is already assigned — it **skips creation entirely when `room_id` is null.** So today, unassigned arrivals generate no pre-arrival cleaning task at all. A3 pre-assigning arrivals closes that gap as a side effect, with no change to the handler.

**Night audit rate derivation** (`src/pms/night_audit/worker.py`) derives the nightly rate from the most recent prior room charge on the folio, and when that resolves to zero it **silently posts nothing**. A2 exists to catch this.

---

## 5. Sub-Agent Interface Contract

Every sub-agent — the five below and every one added later — conforms to the same contract. This is what makes the roster extensible without a redesign.

**A sub-agent declares:**

| Field | Meaning |
|---|---|
| `id` | Stable identifier (`A1`…`An`) |
| `domain` | One-sentence boundary. Narrow enough that outputs don't overlap another sub-agent's. |
| `identity` | The Cognito group and claim scope it runs as |
| `tools` | Explicit allowlist of existing endpoints — reads and writes stated separately |
| `triggers` | Scheduled cadence, platform events, and/or human request types |
| `depends_on` | Other sub-agents that must complete first within a cycle |
| `approval_tier` | `auto` / `propose` / `advise` (§7) — per write, not per sub-agent |
| `output` | Structured findings and proposed actions, plus a human-readable summary |

**A sub-agent receives** the orchestrator's property context snapshot plus any trigger-specific payload.

**A sub-agent returns**, for each cycle:

- **Findings** — observations with evidence. Every finding cites the records it derives from: reservation, folio, task, room, or metric with its comparison window. A finding without a citation is noise and should not be emitted.
- **Proposed actions** — each with target endpoint, arguments, expected effect, reversibility, and a plain-language justification.
- **Escalations** — things it cannot fix, with the reason. These are first-class output, not failures.
- **Confidence** on each proposal, so the orchestrator can route low-confidence items to human review even when the approval tier would allow auto-execution.

**A sub-agent never:** calls another sub-agent, executes a write the orchestrator hasn't cleared, reaches the database directly, or retries past a platform state-conflict rejection.

---

## 6. Identity & Permission Model

Each sub-agent gets a dedicated Cognito service user. The platform's existing `activity_simulator` establishes the pattern: authenticate as a named staff user, cache the token with a refresh buffer, send it as a bearer token, retry once on a reactive `401` with a fresh token.

| Sub-agent | Group | Claim scope | Why this group |
|---|---|---|---|
| A1 Housekeeping Dispatch | `Housekeeping` | `custom:property_id` | Can read the room summary and task queue and assign tasks — and structurally cannot touch billing, loyalty, or reservations |
| A2 Night Audit Exception | `Manager` | `custom:property_id` | Minimum that can read folios *and* post an approved adjustment. Explicitly **not** `Admin` — it has no reason to trigger audit runs |
| A3 Arrivals Readiness | `Manager` | `custom:property_id` | `PUT /stays/{id}/assign-room` is Manager/Admin only |
| A4 Portfolio Insight | `RegionalManager` (per region) and a chain-level `Manager` identity | `custom:region` / none | Read-only; regional scoping is enforced by the reporting handlers themselves |
| A5 Folio Accuracy | `FrontDesk` for the explain-only path; `Manager` for correction proposals | `custom:property_id` | Two identities so the high-frequency guest-explanation path holds no write authority at all |

**Rules that hold for all of them:**

- **No sub-agent runs as `Admin`.** Nothing in the Tier 1 roster requires it.
- Prefer read-only identities wherever advisory output is sufficient.
- Property-scoped claims make cross-property action structurally impossible, not merely disallowed.
- Every write populates the platform's existing provenance fields — `performed_by`, `assigned_to`, `created_by`, `voided_by` — with the agent principal. Rollback of a misbehaving sub-agent is a group change, not a deployment.
- **The agent credentials are standing credentials.** The platform currently ships without enforced MFA, with permissive CORS defaults, a report-only content security policy, and WAF managed rules in count-only mode — all documented as demo-mode exceptions. Every one of those must be tightened before a `Manager`-group agent identity exists in an environment with real guest data. This is a prerequisite, not a follow-up.

---

## 7. Approval Tiers

Not every write deserves the same gate. Tiers are enforced by the orchestrator, per proposed action.

| Tier | Applies to | Gate |
|---|---|---|
| **`auto`** | Housekeeping task assignment and sequencing; pre-arrival room assignment | None. Fully reversible, no guest or money impact, and a human supervisor is already the fallback. |
| **`propose`** | Charges, voids, loyalty adjustments, comps, cross-room-type upgrades | Sub-agent produces the proposal with its evidence chain; a Manager approves in the PMS interface. **Money moves only on a human click.** |
| **`advise`** | Anything with no write path — room status, out-of-order placement, rates, staffing | Ranked recommendation with reasoning. No write attempted. |

The governing rule: **an agent may reorganize work freely and must never move money on its own.** This maps directly onto the platform's existing role guards, which already reserve charges, voids, and loyalty adjustments for Manager and Admin.

Two refinements:

- **Confidence can downgrade a tier but never upgrade one.** A low-confidence `auto` action is routed to `propose`. A high-confidence `propose` action still waits for a human.
- **New sub-agents start at `propose` or `advise`** regardless of their eventual target tier, and are promoted only once the human override rate demonstrates the judgment is sound. Override rate is the trust metric; it should be measured from day one.

---

## 8. The Five Sub-Agents

Ordered by operational impact.

---

### A1 — Housekeeping Dispatch & Room-Turn Optimization

**Highest impact, because housekeeping is the largest controllable labor line in a hotel and the room-turn clock gates every guest-facing promise the property makes.** If a room isn't ready, the front desk absorbs it, the arrival experience degrades, and on a high-occupancy day the property either walks a guest or hands out a comp. The board is re-sequenced dozens of times a day by a supervisor working from a printed list and a radio — a constantly-changing constrained scheduling problem, which is precisely where holding every variable at once beats a heuristic.

The write path already exists and is already correctly guarded. This is a reasoning layer over a complete API.

#### Cycle

Runs on a cadence through the turn window (roughly 08:00–16:00 property-local, every 15–30 minutes) and reactively on `checkinout.checked_out`.

1. Read the board — every room's status, floor, wing, and type; the open task queue.
2. Read demand — today's `CONFIRMED` arrivals with room type, loyalty tier, and pre-assigned room where present; in-house stays classified into stayover vs. departure.
3. Compute the **binding constraint per room type** — rooms ready now versus arrivals needing that type, against the 15:00 default check-in time.
4. Produce an ordered work list per attendant; write assignments.
5. Emit escalations for what it cannot fix.

#### The reasoning a priority-and-age sort cannot do

- **Sequence by arrival pressure, not FIFO.** A dirty King with a pre-assigned Diamond arrival at 15:00 outranks a dirty King with no arrival tonight — even if the second was dirtied three hours earlier.
- **Cluster by floor and wing.** `rooms.floor` and `rooms.wing` exist. Ordering a route to minimize floor changes and cart repositioning is direct, measurable time recovery, and it is entirely invisible to the current system.
- **Resolve type scarcity early.** Four Deluxe arrivals, two Deluxe available, three Deluxe dirty — those three jump the whole queue regardless of their `NORMAL` priority. Otherwise the front desk discovers the problem at 15:00 instead of 09:00.
- **Distinguish stayover from departure.** A stayover refresh is genuinely lower value than a departure turn feeding an arrival, and the `priority` field cannot express that — both are often `NORMAL`.
- **Protect the inspection path.** Tasks sitting in `COMPLETED` awaiting `INSPECTED` are rooms that are physically clean but not sellable. Pure latency with no labor attached, and worth escalating on its own.
- **Respect connecting rooms.** A family arrival needing a connecting pair means *both* rooms must be ready. Sequence them together or not at all — turning one and leaving the other is wasted work.
- **Balance load across attendants.** `assigned_to` being free text is a real limitation, but the agent can still see how many open tasks each name holds and refuse to put the eleventh room on someone holding ten while another holds three.

#### Tools

| Purpose | Endpoint / asset |
|---|---|
| Board state | `GET /housekeeping/rooms/summary` — per-room `{roomId, roomNumber, floor, roomType, status}` + counts |
| Task queue | `GET /housekeeping/tasks`, `GET /housekeeping/tasks/{taskId}` |
| Demand | `GET /stays?propertyId=&status=&date=` |
| Guest tier | `loyaltyTier` on the stays payload |
| Room adjacency | `rooms.floor`, `wing`, `is_connecting`, `connecting_room_id`, `features` |
| **Write** | `PUT /housekeeping/tasks/{taskId}/assign` |
| Reactive trigger | `checkinout.checked_out` |
| Tuning history | Analytics lake: `housekeeping.room_ready` vs `checkinout.checked_out` timestamps → actual turn times by room, floor, and attendant |

#### Approval tier

`auto`. Assignment is fully reversible, involves no money, and a supervisor can override any task through the existing interface. Escalations — no clean room of a needed type, inspection backlog, oversell risk — go to the Manager as advisory.

#### Guardrails

- Only touch tasks in `PENDING` or `ASSIGNED`. The endpoint enforces this with a state-conflict rejection, and that rejection means a human or another process already moved the task. **Authoritative — re-read, never retry.**
- Never reassign a task in `CLEANING`. Someone is physically in the room.
- **Cap reassignments per cycle.** A plan that changes every 15 minutes is worse than a mediocre stable one; churn under an attendant's feet destroys trust faster than any single bad assignment.
- `assigned_to` is capped at 100 characters by the endpoint.

#### Metrics

- **Median and p90 turn time** (checkout → room ready) — the headline number
- **Rooms ready by 15:00** as a share of arrivals needing them
- **Inspection queue latency** (`COMPLETED` → `INSPECTED`)
- Floor changes per attendant shift (route efficiency proxy)
- Arrivals with no ready room of the booked type at check-in — target zero
- **Supervisor override rate** — the trust signal. If it stays high, the agent is wrong, not the supervisor.

---

### A2 — Night Audit Exception & Reconciliation

**Second, because it is recovered revenue and avoided liability, it runs unattended, and the platform hands it a well-defined target.** Night audit is the daily financial close. When it posts wrong, the error compounds silently — into ADR, into RevPAR, into the P&L, into a guest dispute at checkout three days later.

This sub-agent does not replace the deterministic night audit. It runs *after* it and answers the question the deterministic job cannot: **what looks wrong tonight, and what should be done about it?**

#### The ten exception classes

Each is a concrete, findable defect class derived from the existing implementation and schema:

1. **Zero-rate room charges.** The audit derives the nightly rate from the most recent prior room charge on the folio; when that resolves to zero it silently posts nothing, and a stay quietly accrues no revenue. Cross-check every in-house stay against `reservations.amount_per_night` — the rate snapshot taken at booking — and report the exact amount that should have posted.
2. **Missing room-nights.** The room-night guard is `check_in_date <= audit_date < check_out_date`. Any night skipped for any reason leaves a hole in the `charge_date` sequence. A contiguity check per folio finds it in one pass.
3. **Occupied rooms with no checked-in reservation.** Either phantom occupancy suppressing sellable inventory, or a guest in a room with no folio.
4. **Departed guests with open folios.** Reservation `CHECKED_OUT`, folio still `OPEN`. Money on the table that gets harder to collect every day.
5. **`PAYMENT_FAILED` folios and orphaned payment-failure events.** `billing.payment_failed` is documented as *alarmed with no consumer* — nothing acts on it today. A2 becomes its first consumer.
6. **Overstays.** A `CHECKED_IN` stay whose `check_out_date` has passed. The room shows occupied, is not sellable, and may not be accruing charges.
7. **Tax integrity.** Every room charge should have a matching tax line consistent with the configured tax rate. Missing or mismatched tax is a compliance exposure, not just a reporting one.
8. **Duplicate room charges** on the same `folio_id` + `charge_date`. The audit job is idempotent by design; manual posting through the charges endpoint is not covered by that guarantee.
9. **The ADR divergence as a detector.** The night audit computes ADR as revenue ÷ occupied rooms; the daily summary computes it as room revenue ÷ rooms sold. When the two disagree, **the gap is the count of occupied rooms with no active room charge.** The inconsistency is itself a diagnostic — report the delta.
10. **Metric anomalies against history.** `night_audit_runs.metrics` is a JSONB time series keyed uniquely per property-date. Occupancy or revenue breaking materially from the trailing 30 days without a corresponding change in arrivals is worth a human look before the number reaches a report.

#### Cycle

Runs shortly after the deterministic audit completes.

1. Read the run being audited plus trailing history for trend context.
2. Read today's picture: daily summary, in-house stays, room summary, and the affected folios in detail.
3. Work the ten checks, folio by folio for anything suspicious.
4. Produce a **morning exception report ranked by dollar impact**, each item carrying its full evidence chain: reservation, folio, charge lines, expected value, observed value, difference.
5. Propose a specific remediation per item — usually an `ADJUSTMENT` charge whose description cites the reason, or a void of a duplicate line.

#### Tools

| Purpose | Endpoint / asset |
|---|---|
| Audit run + history | `GET /audit/reports/{propertyId}`; `night_audit_runs.metrics` |
| Daily numbers | `GET /reporting/{propertyId}/daily` |
| In-house population | `GET /stays?status=CHECKED_IN` |
| Room state | `GET /housekeeping/rooms/summary` |
| Folio detail | `GET /billing/folios`, `GET /billing/folios/{folioId}` |
| Expected rate | `reservations.amount_per_night`, `total_before_tax`, `total_after_tax` |
| Tax basis | Configured tax rate (default 15%) |
| **Write (gated)** | `POST /billing/folios/{folioId}/charges` — `ADJUSTMENT` only, `OPEN` folios only; `POST /billing/folios/{folioId}/void` |
| Trigger | `audit.night_completed`, or scheduled at 00:30 property-local |
| Trend history | Analytics lake for multi-day event-level reconstruction |

#### Approval tier

`propose`, without exception. Every finding is presented with evidence; a Manager approves before any charge or void. The endpoint constraints reinforce this — `SERVICE` or `ADJUSTMENT` only, `OPEN` folios only, non-zero amount, description ≤500 characters. **The description field is where the reason goes, so the justification lives with the transaction permanently.**

#### Guardrails

- **Never post `ROOM_RATE`.** That is the audit job's responsibility, and the endpoint doesn't permit it. Corrections go in as `ADJUSTMENT`.
- Read the folio immediately before proposing, and again before executing an approved action. Folios change during a shift.
- One proposal per exception per audit date, keyed on `(folio_id, charge_date, exception_type)`. Re-running must not double-propose.
- **Report findings even when no remediation exists** — a phantom `OCCUPIED` room has no write path, and saying so is more useful than silence.
- Property-scoped. The regional roll-up belongs to A4.

#### Metrics

- **Dollar value of recovered charges** per property per month — the number that justifies the whole program
- Exceptions per 100 room-nights, trending down as root causes get fixed
- Zero-rate room-charge incidents — should reach zero once surfaced
- Open folios on departed guests, aged
- Time from exception occurrence to resolution
- Night-audit-driven billing disputes at checkout
- **False-positive rate** (Manager rejection rate on proposals)

---

### A3 — Arrivals & Front-Desk Readiness

**Third, because it replaces a demonstrably naive algorithm with one the data already supports, and it improves the arrival experience — the moment that sets the tone for an entire stay — while cutting front-desk handling time.**

The existing assignment is highest floor, lowest room number, first available room of the type. It is a placeholder, and every input needed to do better is already in the database.

#### Cycle

Runs twice daily — an evening or 06:00 sweep for the full arrivals list, and a 14:00 re-check before the 15:00 check-in time — plus reactively on `reservation.created` for same-day bookings.

1. Read arrivals for the target date.
2. Enrich each: guest `preferences`, `tags`, `vip_level`, `total_stays`, `last_stay_date`, `language`; loyalty tier and balance.
3. Read the inventory picture: per-room status, floor, type; room-type attributes (`accessibility_type`, `smoking_allowed`, `max_occupancy`, `bed_configuration`).
4. **Solve the assignment as a whole set, not greedily one guest at a time.** This is the core improvement — greedy assignment gives the best room to whoever is processed first and strands later high-value arrivals.
5. Write assignments.
6. Produce a **front-desk arrival brief**: who's arriving, who matters, what's been arranged, what's at risk.

#### Matching logic

- **Preference matching.** `guests.preferences` JSONB (high floor, quiet, away from the elevator, bed type) against `rooms.floor`, `rooms.features`, `rooms.wing`, and `room_types.bed_configuration`. Free-text preferences are exactly what a language model handles better than a schema — "prefers a quiet room away from the ice machine" has no column, but `features` and `wing` can often satisfy it.
- **Accessibility as a hard constraint.** `room_types.accessibility_type` is honored, never traded off. Same for `smoking_allowed`.
- **Occupancy fit.** `adults + children` against `max_occupancy`; families with children get connecting rooms via `is_connecting` / `connecting_room_id`.
- **Tier-aware allocation.** `loyalty_tier` and `vip_level` decide who gets the genuinely better room when supply is short.
- **Recognition of returning guests.** `total_stays` and `last_stay_date` identify them, and **returning a guest to the same room or the same floor as their last stay is the kind of recognition guests actually notice** — reconstructible from `checkinout_records` or the event archive.
- **Room-ready feasibility.** Don't assign a room that is `DIRTY` with no task or an unrealistic turn time. This is where A3 and A1 compose through the orchestrator: A3's assignments become A1's priorities, and A1's completion forecast constrains A3's choices.
- **Spread the housekeeping load.** All else equal, distribute across floors so tomorrow's checkout turn isn't concentrated in one section.
- **Type-scarcity detection.** Count arrivals per room type against available plus turnable inventory, and flag shortfalls hours before the front desk hits them.

#### Tools

| Purpose | Endpoint / asset |
|---|---|
| Arrivals | `GET /stays?propertyId=&status=CONFIRMED&date=` |
| Guest profile | `GET /guests/{guestId}` → `preferences`, `comm_prefs`, `tags`, `vip_level`, `total_stays`, `total_spend`, `last_stay_date`, `language` |
| Loyalty | `GET /loyalty/{guestId}`, `GET /loyalty/{guestId}/transactions` |
| Room inventory | `GET /housekeeping/rooms/summary`; `rooms.floor/wing/features/is_connecting/connecting_room_id` |
| Room-type constraints | `GET /properties/{propertyId}/room-types` |
| Party size & requests | `reservations.adults`, `children`, request flags, `additional_notes`, `attributes` |
| Oversell check | `availability.available`, `overbooking_allowance` |
| **Write** | `PUT /stays/{reservationId}/assign-room` |
| Stay history | `checkinout_records`; analytics lake for prior room numbers |
| Trigger | Scheduled sweeps + `reservation.created` |

#### A useful property of the write endpoint

`pre_assign_room.py` accepts a room of a **different room type** as a deliberate Manager override — it logs the mismatch and allows it. That is real capability: it lets A3 execute an upgrade for a high-tier guest when the booked type is short, rather than being blocked by a type check. It also means A3 must be conservative and explicit about using that latitude, and must state the upgrade in the brief so the front desk isn't surprised at the counter.

The endpoint also returns status `PRE_ASSIGNED`, accepts only `CONFIRMED` reservations, returns a conflict if a room is already set, and requires the room to be `AVAILABLE` in the same property.

#### Side benefit

Because pre-arrival housekeeping tasks are only created when a room is already assigned, **pre-assigning arrivals the evening before generates `PRE_ARRIVAL` tasks that don't exist today** — with no change to the event handler. A3 feeding A1 is not just coordination; it materially increases the work A1 has visibility into.

#### Approval tier

`auto` for same-type assignment — it is reversible (a room move endpoint exists for in-house guests, and re-assignment before arrival is trivial) and carries no financial impact. **`propose` for cross-room-type upgrades** initially, promoted to `auto` once the override rate proves the judgment. The arrival brief itself is `advise`.

#### Guardrails

- Accessibility and smoking constraints are **hard** and never traded for preference optimization.
- An already-assigned conflict means a human chose that room. Leave it.
- Room must be `AVAILABLE` and in the same property — endpoint-enforced, but verify first to avoid noisy failures.
- Cross-type assignment requires an explicit logged reason.
- **Don't churn.** Once assigned and communicated, don't reshuffle absent a real constraint change.
- **Never assign one room to two arrivals** — read the full arrival set atomically per cycle and track within-cycle assignments.

#### Metrics

- Average check-in handling time (front-desk seconds per arrival)
- **Room moves within 24 hours of check-in** — a direct measure of bad assignment
- Preference-match rate for guests with populated `preferences`
- Returning guests placed on a previously-stayed floor
- Arrivals with no ready room of the booked type at 15:00
- Upgrade cost: cross-type assignments and their rate delta
- `PRE_ARRIVAL` tasks created — should jump materially once pre-assignment runs

---

### A4 — Portfolio Insight & Operations Command Brief

**Fourth, because this is where leverage multiplies.** A regional director covering 15 properties, or a chain lead covering 50, cannot read 50 dashboards. Today they get numbers; what they need is *which three properties need attention this morning and why*.

It ranks below A1–A3 because it does not directly change an operational outcome — it changes where human attention goes. High value, one step removed.

#### Cycle

Runs early each morning, per region and chain-wide, after A2 has completed for the properties in scope.

1. Cross-property snapshot: per-property occupancy, revenue today, check-ins and check-outs, chain summary.
2. Trend baseline: up to 92 days of per-day revenue, room-nights, occupancy, arrivals, and cancellations.
3. Last night's audit metrics per property.
4. Drill-down daily summaries for properties that warrant one.
5. Analytics-lake queries for cross-property behavioral history the reporting endpoints don't expose — actual turn times by property, cancellation timing distributions, payment-failure clustering.
6. Produce a ranked brief in **exception-severity order — not alphabetical, not by property size.** Each item names the property, the deviation, the probable driver, and the specific action.

#### The reasoning that matters

- **Deviation, not absolutes.** 72% occupancy is excellent for one property and alarming for another. The comparison is against that property's own trailing 30 days and its same-weekday history — never a chain average.
- **Correlate across dimensions.** Occupancy down *and* cancellations up is demand. Occupancy flat *and* revenue down is rate, or a charge-posting failure. Occupancy up *and* revenue flat is very likely A2's zero-rate defect. The reporting API returns all three; nothing reads them together today.
- **Aggregate the exception queue.** Once A2 runs per property, the chain view becomes "which properties generate the most financial exceptions" — a signal about process quality, not just numbers.
- **Separate systemic from local.** Fifteen properties with the same anomaly on the same night is a platform problem — check the dead-letter queues. One property with it is an operations problem. **Telling these apart correctly is worth the entire sub-agent**, because the response is completely different.
- **Reconcile the ADR divergence** between the audit metrics and the daily summary, and treat the gap as the diagnostic it is.

#### Tools

| Purpose | Endpoint / asset |
|---|---|
| Cross-property snapshot | `GET /reporting/occupancy` |
| Trend | `GET /reporting/range` (≤92 days, `_all` sentinel, region-filtered for regional callers) |
| Per-property depth | `GET /reporting/{propertyId}/daily`, `GET /audit/reports/{propertyId}` |
| Directory | `GET /properties` (incl. `region`) |
| Behavioral history | Analytics lake: Glue `events` table via the dedicated Athena workgroup |
| Upstream findings | A2 output, via the orchestrator |
| Trigger | Scheduled daily, plus a weekly roll-up |

#### Approval tier

`advise`. This sub-agent writes nothing, and that is the correct scope — chain-level automated writes across 50 properties is a blast radius nobody should accept from a first version.

#### Guardrails

- `GET /reporting/range` caps at 92 days. Chunk longer analyses or use the analytics lake.
- **Analytics queries must be partition-bounded.** Always constrain source, year, month, and day. An unpartitioned scan of the event lake is a real cost incident.
- Every claim cites its source metric and comparison window. **A brief that cries wolf gets ignored within a week** — unsourced anomaly claims are worse than no brief.
- Regional identities cannot see outside their region; the reporting handlers enforce it.

#### Metrics

- Time from anomaly occurrence to management awareness
- Regional-director time spent assembling status versus acting on it
- **Precision of flagged properties** — did the flagged property actually have an issue?
- Systemic issues caught chain-wide before individual properties reported them

---

### A5 — Folio Accuracy & Billing Explanation

**Fifth: high value, with deliberate overlap on A2.** Where A2 is a nightly batch sweep, A5 is a *per-folio, on-demand* auditor — invoked before checkout, when a guest questions a charge, or when a folio is flagged.

It earns a separate slot because the moment matters. A wrong charge caught before the guest sees it is a non-event; the same charge caught after is a dispute, a service recovery, a chargeback, and a review. And a front-desk agent explaining a charge at 07:30 has about ninety seconds.

#### Cycle

For a given folio:

1. Read the folio with every charge line — type, amount, `charge_date`, status.
2. Read the booking snapshot: `amount_per_night`, `total_before_tax`, `total_after_tax`, `refundable`, `guarantee_type`, booked rate-plan and room-type.
3. Reconcile line by line:
   - One `ACTIVE` room charge per night of stay — no gaps, no duplicates on the same `charge_date`
   - Each rate matches `amount_per_night`, or the variance is explained (rate-plan change, documented upgrade)
   - Tax lines consistent with the configured rate
   - `SERVICE` charges carry descriptions that are actually explainable to a guest
   - `ADJUSTMENT` lines carry a stated reason
   - Voided lines have `voided_by` recorded
   - Folio total reconciles to `total_after_tax` within the variance explained by posted services and adjustments
4. Produce a **plain-language explanation of every line** — the artifact a front-desk agent can read to a guest — plus discrepancies with proposed corrections.

#### Why the booking snapshot makes this tractable

`reservations` stores the rate *as sold*. That means "what should this folio total?" has a definitive answer that doesn't depend on reconstructing rate-plan state as of the booking date. **Most PMS platforms cannot answer this cleanly. This one can, and that is what makes an automated auditor reliable rather than approximate.**

#### Tools

| Purpose | Endpoint / asset |
|---|---|
| Folio + lines | `GET /billing/folios`, `GET /billing/folios/{folioId}` |
| Booking snapshot | `reservations.amount_per_night`, `total_before_tax`, `total_after_tax`, `refundable`, `guarantee_type`, booked codes |
| Stay dates | `GET /stays` → `checkInDate`, `checkOutDate`, `checkedInAt`, `checkedOutAt` |
| Tax basis | Configured tax rate |
| Payments | `payments` status (`APPROVED`/`DECLINED`/`REFUNDED`); `GET /payments/{paymentId}` |
| Refund taxonomy | `payment_refunds.reason` — constrained to `EARLY_DEPARTURE`, `SERVICE_RECOVERY`, `BILLING_ERROR`, `CANCELLATION` |
| **Write (gated)** | `POST /billing/folios/{folioId}/charges` (`ADJUSTMENT`), `POST /billing/folios/{folioId}/void` |
| Trigger | On-demand from the PMS interface; `billing.payment_failed`; pre-checkout sweep |

#### Approval tier

`propose` for all corrections; `advise` for the guest explanation, which is the highest-frequency and lowest-risk output. The refund reason taxonomy is constrained by a database check — the sub-agent must classify into one of the four existing values, which is a useful forcing function for consistent reason coding.

#### Guardrails

- Never post or void without approval. The endpoint's role guard is the backstop; the design is the primary control.
- **Guest-facing explanations must not speculate.** If a line can't be explained from the data, say "requires front-desk review." A confidently wrong explanation to a guest is worse than an honest gap.
- `OPEN` folios only for charge posting (endpoint-enforced); amount non-zero; description ≤500 characters.
- **Void and re-post rather than mutating a charge.** The platform has no charge-update path, and that immutability is a feature.

#### Metrics

- Billing disputes per 1,000 checkouts
- **Discrepancies caught pre-checkout versus post-checkout** — the ratio is the whole point
- Front-desk time per billing inquiry
- Chargebacks and post-stay refunds coded `BILLING_ERROR`
- Folio-to-snapshot variance rate

---

## 9. Orchestration Cycles

The concrete daily choreography. Times are property-local.

| Cycle | Time | Sub-agents | Order | Output |
|---|---|---|---|---|
| **Post-audit review** | ~00:30, after the night audit completes | A2 | — | Morning exception report, ranked by dollar impact, with proposed corrections queued for Manager approval |
| **Portfolio brief** | ~06:00, after A2 across the region | A4 | depends on A2 | Regional and chain brief in exception-severity order |
| **Morning arrivals sweep** | ~06:00 | A3 → A1 | A3 first | Today's assignments written; front-desk arrival brief; A1 re-sequences around the new assignments |
| **Turn window** | 08:00–16:00, every 15–30 min | A1 | — | Continuously re-sequenced housekeeping board; escalations on type scarcity and inspection backlog |
| **Pre-arrival re-check** | ~14:00 | A3 → A1 | A3 first | Late-breaking assignment fixes before the 15:00 check-in time |
| **Evening pre-assignment** | ~18:00 | A3 → A1 | A3 first | Tomorrow's assignments — which also generates tomorrow's `PRE_ARRIVAL` housekeeping tasks |
| **Pre-checkout sweep** | ~06:00, or per-stay before checkout | A5 | — | Per-folio explanation and discrepancy list for today's departures |

**Reactive triggers:**

| Platform event | Sub-agent | Why |
|---|---|---|
| `checkinout.checked_out` | A1 | A room just went dirty; the board changed |
| `reservation.created` | A3 | Same-day booking needs an assignment now |
| `billing.payment_failed` | A5 (and A2 at close) | Currently has **no consumer at all** — this becomes its first |
| `audit.night_completed` | A2 | Starts the post-audit review |

**On-demand from a human:** a Manager or front-desk user asks a question or requests an action. The orchestrator decides which sub-agents contribute, runs them, and returns one merged answer. This path is how the agent gets used day-to-day, and it should be treated as a first-class entry point rather than an afterthought to the scheduled cycles.

**Cycle discipline:**

- The orchestrator builds the property context snapshot **once per cycle** and passes it to every participating sub-agent. Two sub-agents reasoning over snapshots taken ninety seconds apart is a conflict generator.
- Sub-agents within a cycle that have no `depends_on` relationship may run concurrently; the orchestrator still serializes writes to any single entity.
- A cycle that overruns its next scheduled start is skipped, not queued. Stale plans are worse than missed ones.

---

## 10. Cross-Cutting Guardrails

Applies to the orchestrator and every sub-agent, present and future.

**Authorization**
- One dedicated Cognito identity per sub-agent, in the least-privileged group that can do the job. No sub-agent runs as `Admin`.
- Property-scoped claims wherever the domain is property-scoped. Cross-property action becomes structurally impossible.
- **Never reach the database directly.** Direct data access would bypass tenant scoping, endpoint validation, and the audit trail. Everything goes through the API, like any other client.

**Correctness**
- **Read, verify current state, then write.** Never write from a stale snapshot.
- **A platform state-conflict rejection is authoritative.** It means a human or another process acted. Re-read and re-plan; never retry over it.
- Idempotency keys on every proposal so a re-run never double-acts: `(entity_id, date, action_type)` at minimum.
- The platform owns validation. If a proposed action is rejected on business grounds, the sub-agent's model of the world was wrong — log it as a learning signal, don't work around it.

**Money**
- No sub-agent moves money without human approval, regardless of confidence.
- Corrections are posted as `ADJUSTMENT`, never as a re-post of a system-generated charge type.
- Every financial proposal carries its full evidence chain, and the reason is written into the transaction's own description field so the justification is permanent and co-located.

**Evidence and honesty**
- Every finding cites the records it derives from. Uncited findings are not emitted.
- **Escalations are first-class output.** "I found this and cannot fix it" is a valuable result, not a failure.
- Guest-facing text never speculates. Unexplainable means "requires review."

**Stability**
- Cap changes per cycle. Churn destroys operational trust faster than any single wrong decision.
- Once a decision is communicated to a human or a guest, don't silently reverse it.

**Observability**
- A **decision log** per run: run identifier, property, cycle type, inputs hash, findings, proposals, actions taken, human overrides. This is what makes recommendation quality measurable rather than anecdotal, and it is the input to every promotion decision in §7.
- Human override rate tracked per sub-agent and per action type, from the first day of the first pilot.

**Security prerequisites** (§6) — enforced MFA, restricted CORS, enforcing content-security policy, and WAF rules moved out of count-only mode must all be in place before any agent identity with write authority exists in an environment holding real guest data.

---

## 11. Explicitly Out of Scope

Stated plainly so the roadmap stays honest. Each of these would require changing the platform, adding a subsystem, or sourcing external data — which violates the hard constraint in the header.

| Gap | Consequence |
|---|---|
| **No inbound guest channel** | Email is outbound only; no SMS, chat, or reply webhook. Any capability triggered by "the guest says…" needs a staff-entered or ops-derived trigger instead. |
| **No point-of-sale** | No outlets, tables, checks, or menu items. Food, beverage, and spa revenue can only enter as a `SERVICE` charge. |
| **No rate or availability write API** | Rate and availability tables exist but are populated by seed scripts. **No endpoint changes a price or an allocation.** This rules out revenue and rate optimization entirely. |
| **No channel manager or OTA integration** | Booking source is recorded; nothing is pushed outbound. |
| **No room-status write endpoint** | Room status is mutated only by check-in, check-out, and the housekeeping workflow. An agent cannot place a room out of order. |
| **No staff roster, skills, or shift data** | `assigned_to` is free text. A1 therefore optimizes **sequence and grouping**, never headcount, shift assignment, or productivity targets. |
| **No maintenance work-order system** | Only a `MAINTENANCE` task type. No asset registry, parts, vendors, or SLAs. |
| **No group blocks table** | `reservations.group_block_id` is a column with no referenced table. |
| **No survey, review, or NPS data** | No sentiment source inside the platform. |
| **No external data feeds** | No flight arrivals, weather, competitor rates, city events, or demand forecasts. |
| **Nothing sets `NO_SHOW`** | The status value exists; no code path assigns it. |
| **No vector store or knowledge base** | No retrieval substrate for SOPs or brand standards. |

**Two internal inconsistencies** worth fixing in the platform independently, and worth knowing about because A2 and A4 will surface them:
- ADR is computed as revenue ÷ occupied rooms in the night audit, and room revenue ÷ rooms sold in the daily summary. These diverge whenever an occupied room has no active room charge.
- The loyalty tier enumeration lists `DIAMOND`; the guest `vip_level` check constraint lists `PLATINUM`.

---

## 12. Future Sub-Agents

Candidates that fit the §5 contract. Listed to show the roster is designed to grow, not as commitments.

**Reachable with no platform change:**

| Candidate | Domain | Note |
|---|---|---|
| **A6 Operations Copilot** | Conversational read-only Q&A across every existing read endpoint plus the analytics lake | Runs under the *human's* session token, so it inherits their exact permissions. The cheapest sub-agent to add and the most-used. |
| **A7 Platform Health & Stuck-Workflow Triage** | Dead-letter queue depth, alarm state, workflows suspended on unreleased task tokens, stuck task states | Narrower operational value, but it is what tells A4 that an anomaly is systemic rather than local. |
| **A8 Loyalty Integrity** | Tier thresholds versus `total_stays`, points ledger versus balance, earn multipliers versus tier | Read-heavy; adjustments are `propose` only. |

**Needs one small platform addition** — deliberately deferred:

| Candidate | Blocked on |
|---|---|
| Late-checkout / early-check-in decisioning | Request flags exist on `reservations`; no endpoint mutates them |
| Room inventory health, stuck states, out-of-order lifecycle | No room-status write endpoint |
| No-show and late-arrival resolution | Nothing sets `NO_SHOW` |
| Guest service recovery | No inbound channel and no survey data — needs a trigger surface |

---

## 13. Roadmap

**Phase 0 — Foundations for the agent layer**
Orchestrator skeleton: property context assembly, sub-agent registry, dependency graph, approval-tier enforcement, decision log. The tool layer as thin wrappers over existing endpoints, with the platform's error envelope passed through verbatim. Cognito service identities per §6. Security prerequisites closed out before any write authority is granted. **No sub-agent logic yet** — this phase exists so that adding one is a registration.

**Phase 1 — A6 Operations Copilot, read-only**
Build the future sub-agent first, deliberately. It exercises the entire tool layer, the identity model, and the orchestrator's routing with **zero write risk**, and it produces immediate visible value. It is the cheapest possible proof that the reads, the scoping, and the analytics access all work. Everything after this is incremental.

**Phase 2 — A1 Housekeeping Dispatch**
Highest-impact sub-agent, `auto` tier, single pilot property. Instrument turn time and supervisor override rate from the first cycle. Do not expand until the override rate is stable and low.

**Phase 3 — A2 Night Audit Exception**
`propose` tier throughout. Report-only for the first two weeks — findings with no proposals — to establish the false-positive rate before any correction reaches a Manager's queue. The dollar value of recovered charges from this phase is what funds the rest.

**Phase 4 — A3 Arrivals Readiness**
Introduces the first real cross-agent dependency (A3 → A1) and exercises the orchestrator's sequencing and conflict resolution. Same-type assignment at `auto`; cross-type upgrades at `propose` until proven.

**Phase 5 — A5 Folio Accuracy**
Largely a new prompt, trigger, and guest-facing output format over A2's tool set. Start with the explain-only path on a `FrontDesk` identity — no write authority at all — then add correction proposals.

**Phase 6 — A4 Portfolio Insight**
Requires A2 running across multiple properties to be worth much, which is why it is last despite ranking fourth on impact. Adds the analytics-lake tool and the regional identity.

**Phase 7 — Expansion**
A7, A8, and whichever deferred candidates justify a small platform addition. By this point adding a sub-agent should be a registration plus a prompt plus a tool allowlist.

Two notes on ordering. First, **the build order is not the impact order** — A6 is built first because it de-risks everything, and A4 is built last because it depends on A2's coverage. Second, each phase should not begin until the prior phase's override rate has been measured. The temptation to run phases concurrently is strong and should be resisted: the whole program's credibility rests on the first `auto`-tier sub-agent being visibly trustworthy.

---

## 14. Open Decisions

Deliberately unresolved, and not blocking Phase 0:

1. **Agent runtime and framework.** Where the orchestrator and sub-agents execute, how the agent loop is implemented, and which model provider and configuration are used. Nothing in this document depends on the answer — the tool layer is HTTPS calls against documented endpoints and the identity model is Cognito, both of which are runtime-agnostic.
2. **Scheduling mechanism** for the cadence cycles in §9, and whether reactive triggers consume the existing event bus directly or through an intermediary.
3. **Human approval surface** for the `propose` tier — extending the existing PMS interface versus a separate queue.
4. **Decision-log storage and retention**, and whether it feeds the analytics lake alongside platform events.
5. **Pilot property selection** for Phase 2, and the target override-rate threshold that gates expansion.
