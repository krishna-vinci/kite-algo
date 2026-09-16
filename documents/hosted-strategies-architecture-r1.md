# Hosted and External Strategy Architecture — Draft R1

**Status:** Design draft under review
**Product target:** Hosted Strategies Product V1
**Architecture revision:** R1 (this is an *architecture revision*, not Product Version 1)
**Date:** 2026-09-16
**Supersedes:** the exclusive-instrument-claim recommendation from the previous consolidated design audit (see §18 Corrections Ledger)
**Follow-on:** R2, R3, … via later Grill Me sessions; a Product V1 specification and a task-level implementation plan are created only after the architecture revisions are accepted.

**Historical inputs preserved (not rewritten, not deleted):**

- `documents/hosted-strategies-design.md`
- `documents/hosted-strategies-implementation-plan.md`
- `documents/hosted-strategies-supervisor-slice-report.md`
- `documents/hosted-strategies-foundation-report.md`
- `documents/hosted-strategies-proposal-draft.md` (DRAFT proposal, untracked)
- `documents/live-paper-accounting-and-worker-live-execution.md`
- `docs/superpowers/specs/2026-04-10-option-strategy-centralization-design.md`
- `docs/superpowers/specs/2026-04-23-strategy-run-unification-design.md`
- `docs/superpowers/specs/2026-04-24-live-paper-accounting-and-reconciliation-design.md`
- `docs/superpowers/specs/2026-04-24-risk-schema-current-state-and-next-steps.md`
- `docs/superpowers/specs/2026-04-25-centralized-exposure-protection-design.md`
- `docs/superpowers/specs/2026-04-28-sdk-order-execution-validation-design.md`
- `docs/superpowers/specs/2026-04-29-generic-live-protection-100-gate-design.md`
- `docs/superpowers/specs/2026-05-06-option-strategy-execution-core-design.md`
- `docs/superpowers/specs/2026-05-06-unified-run-safety-check-design.md`
- `docs/superpowers/specs/2026-05-06-worker-runtime-reliability-recovery-design.md`
- `docs/superpowers/specs/2026-05-07-worker-attribution-hardening-and-bracket-intents-design.md`
- `docs/superpowers/specs/2026-05-07-worker-execution-observability-and-basket-state-design.md`

**Evidence rule for this document.** Every Implemented / Partial / Missing / Incompatible / Unsafe claim carries a `path:line` citation. Citations were produced by direct source reads and targeted searches during this revision. Where a prior document's claim conflicts with source, the source wins and the conflict is recorded in §1.4.

---

## 0. Revision rules

1. R1 fixes vocabulary and boundaries. It does not schedule work.
2. Anything marked **PROPOSED** is not implemented and must not be assumed by readers of the SDK or the UI.
3. Anything marked **SUPERSEDED** was decided in an earlier session and is explicitly replaced here.
4. Open product questions are recorded in §17 and are **not** silently resolved by omission.
5. No production code, migration, deployment, order or notification accompanies this document.

---

## 1. Reconciliation of prior documents

### 1.1 Hosted-strategy documents

| Document | True status | Evidence / conflict |
|---|---|---|
| `hosted-strategies-design.md` | Implemented foundation; several listed capabilities are deferred, and the document says so itself | Header claims IMPLEMENTED AND DEPLOYED (`:3-8`); `resolve_futures_contract` is marked PROPOSED (`:215-217`); options reuse limited to the state machine (`:219-220`, `:301`) |
| `hosted-strategies-implementation-plan.md` | Plan of record; one internal contradiction | `:3-5` says every slice was implemented, while `:149` records the first bounded slice as NOT started; treat `:149` as the stale line (schema and authorization foundation verifiably exist — `backend/schema.sql:2286`, `:2371`) |
| `hosted-strategies-supervisor-slice-report.md` | Shipped report; carries the authoritative deferral list | Scheduling not implemented (`:576-583`); cancel/flatten not implemented (`:903-904`); live activation not enabled (`:700-709`); futures resolver and options mode propagation out of scope (`:584-587`) |
| `hosted-strategies-foundation-report.md` | Slice 0 only — schema + authorization | "creates no execution path" (`:9`); no runner/orders (`:39-42`) |
| `hosted-strategies-proposal-draft.md` | DRAFT proposal (2026-09-11), superseded by `design.md` | `:3` DRAFT; open questions `:242-256` |

### 1.2 Accounting and worker-live documents

`live-paper-accounting-and-worker-live-execution.md` (2026-04-24) remains accurate on the live path: attribution is injected by the backend rather than trusted from the worker (`:12`, `:55`), `live_order_intents` is persisted with attribution and broker order id (`:21-27`), `order_state_projection` is seeded `needs_reconcile` (`:31`), and untagged broker-side exits attach to a strategy only when **exactly one** open live run matches the account/instrument/product and the fill reduces that run (`:37`). That last rule is the origin of the V1 boundary in §3.3.

### 1.3 Spec-to-source audit (summary)

| Spec | Verdict | Anchor |
|---|---|---|
| Strategy-run unification (04-23) | Partial — mandatory `strategy_run_id` and capability contract exist; `StrategyTemplate`/`StrategyLeg`/`StrategyEvent` classes and `draft`/`pending_entry` states do not | `backend/schema.sql:953-971`, run status CHECK at `:959`; builder at `backend/options/execution/runtime_updates.py:43-59` |
| Option centralization (04-10) | Implemented | `backend/options/strategy/models.py:15-32`, `:41-77`; `option_strategy_runs` at `backend/schema.sql:1191-1207` |
| Option execution core (05-06) | Implemented | `backend/options/execution/models.py:12-22`, `:52-100`; `option_run_states` at `backend/schema.sql:1212` |
| Risk schema note (04-24) | Current state implemented; "next steps" not implemented | `backend/api/routers/worker_protection.py:1038-1049` stores risk; no enforcement |
| Attribution + bracket intents (05-07) | Implemented | `worker_live_execution_links` at `backend/schema.sql:1402`; bracket tables at `:1426`+ |
| Execution observability + basket state (05-07) | Implemented | `basket_executions` `backend/schema.sql:1019`, legs `:1042` |
| Worker runtime reliability (05-06) | Implemented | session nonce/heartbeat columns at `backend/schema.sql:965-967` |
| Unified safety check (05-06) | Implemented, and exceeded its own v1 | `backend/api/routers/worker_protection.py:824-876`; options evaluation now run-state |
| Centralized exposure protection (04-25) | Partially adopted; caps excluded by that spec itself | `:389-400` |

### 1.4 Documentation that no longer matches source

1. `hosted-strategies-implementation-plan.md:3-5` vs `:149` (internal contradiction, §1.1).
2. `2026-05-06-unified-run-safety-check-design.md` §7.2/§11.1 describes options evaluation and option enter/exit token enforcement as deferred; both are implemented (`backend/options/api/worker_options_router.py:256-257`, `:292-293`).
3. The earlier consolidated audit's **exclusive instrument claim** recommendation is **SUPERSEDED** — see §3 and §18.
4. `documents/hosted-strategies-design.md:5` "IMPLEMENTED AND DEPLOYED" over-states the slice: scheduling, cancel and flatten are absent (report `:576-583`, `:903-904`).

---

## 2. Current-state audit (evidence)

### 2.1 Durable run, job, attempt, session

- Run: `algo_worker_runs` (`backend/schema.sql:953-971`) with `strategy_run_id`, `token_id`, `template_id` (`hosted:<strategy_id>`), `account_scope`, `execution_mode`, status `open|paused|exiting|closed|failed` (`:959`), `runtime_state_json`, `risk_schema_json`, `allowed_actions_json`, `summary_fields_json`, `metadata_json`.
- Job ledger: `strategy_jobs` (`backend/schema.sql:2371`) with `strategy_id`, `version_id`, `owner_id`, `account_scope`, `job_kind`, `execution_mode`, `desired_state`, `occurrence_key` UNIQUE, `run_id` TEXT, `token_id`, `lease_owner`, `lease_epoch`, `attempt`, `status` (default `queued`), `recovery_required` semantics documented at `backend/strategies/models.py:184-190`.
- Schedule: `hosted_strategy_schedules` (`backend/schema.sql:2332`) — **stored only**, `job_kind IN ('continuous','finite')`, `schedule_kind IN ('daily','weekly')` only, `execution_mode IN ('paper','dry_run')`, `at_time`, `weekday`, `timezone` default `Asia/Kolkata`, `window_end`, `squareoff_at`, `manual_paused_at` (`backend/strategies/models.py:115-181`).
- No runner, scheduler or notification code by design: `backend/strategies/__init__.py:14`.
- Session: claim/release/heartbeat (`backend/api/routers/worker_auth.py:237-239`); single-winner conditional update in `backend/api/repositories/algo_worker_repo.py:778-880`.
- Recovery: a started attempt that loses its lease is fenced to `recovery_required`; there is **no resume/reattach** (`backend/api/services/runtime_recovery.py:167-331`).

### 2.2 What strategy code emits today

| Channel | Endpoint | Status |
|---|---|---|
| Order / basket intent | `POST /worker/runs/{id}/intents` (`intent_type=place_order|place_basket`) — `backend/api/routers/worker_execution.py:809` | Implemented |
| Bracket (OCO) | `POST /worker/runs/{id}/brackets` — `backend/api/routers/worker_execution.py:640` | Implemented |
| Exit run | `POST /worker/runs/{id}/exit` — `backend/api/routers/worker_execution.py:944` | Implemented (paper + live; cancel/flatten per-action not implemented) |
| Options run | `POST /worker/options/runs`, `…/enter`, `…/exit` — `backend/options/api/worker_options_router.py:196`, `:242`, `:278` | Implemented, **hosted = paper only** (`:80-87`, `:220-227`) |
| Protection | `PATCH /worker/runs/{id}/protection` — `backend/api/routers/worker_protection.py:1051` | Implemented |
| Risk patch | `PATCH /worker/runs/{id}/risk` — `backend/api/routers/worker_protection.py:1038-1049` | Implemented but **storage only** |
| Notify | `POST /worker/runs/{id}/notify` — `backend/api/routers/worker_execution.py:992` | Implemented |
| Targets / weights | — | **Missing.** `sdk/python/kite_algo_worker/endpoint_manifest.py:53-95` contains no target endpoint; the only `target_weight_pct` in the tree is dead ORM (`backend/broker_api/broker_api.py:192-204`, referenced nowhere) |

Envelope shape (implemented): `{intent_type, payload: {order|basket}, idempotency_key (8–160 chars), metadata, safety_token?}`; dedupe on `(strategy_run_id, idempotency_key)` — `backend/schema.sql:1013`, `backend/api/repositories/algo_worker_repo.py:1723`.

### 2.3 The live attribution chain (this is the foundation of §3)

1. Order placed with attribution injected by the backend: `backend/api/routers/worker_execution.py:151-171`, `:203-206`.
2. `live_order_intents` row (`backend/schema.sql:1363`), broker `tag = client_order_ref`; client-order-ref factory `backend/broker_api/orders/live_order_intents.py:30`; create/mark/seed at `:77`, `:151`, `:174`.
3. Execution link written: `worker_live_execution_links` (`backend/schema.sql:1402`) with `broker_order_id`/`trade_id` uniqueness (`:1415-1421`).
4. Fills stored: `order_trade_fills` (`backend/schema.sql:634`), written at `backend/broker_api/orders/order_runtime.py:578`, applied at `:639`.
5. Account position updated from fills and from broker reconcile: `account_positions` (`backend/schema.sql:658`), reconciled at `order_runtime.py:865`.
6. **Per-run attributed net quantity** derived by SQL: `backend/api/repositories/algo_worker_repo.py:1212-1277` — `SUM(BUY)-SUM(SELL)` over fills joined to attributed broker orders, returning both `net_quantity` and `broker_net_quantity` (`:1255-1272`).
7. Exit orders generated **from the attributed quantity only**: `backend/api/routers/worker_execution.py:231-251`.

**Conclusion:** per-strategy attributed quantity already exists and is exact for fills that are linked. It is a derived read model, not a persisted ledger.

### 2.4 Exit path — implemented behavior

`_exit_live_worker_run` (`backend/api/routers/worker_execution.py:272-401`):

1. refresh account state (`:282`), load attributed open legs (`:283`);
2. if no attributed legs: consult `list_live_strategy_broker_positions` then `_live_broker_positions_for_attribution`; if any position exists → `status: "deferred"` (`:285-307`); else close as flat (`:308-317`);
3. otherwise validate (`:319` → `_validate_live_exit_legs` `:208-229`) and submit a basket of exits sized to attributed quantity (`:345-370`);
4. close only when no attributed legs remain after post-submit refresh (`:376-391`), else `exiting`.

Validation rule (`:220-229`): for a long, require `broker_net >= attributed_net`; for a short, require `broker_net <= attributed_net`. **Same-direction overlap passes; opposing exposure is refused.**

### 2.5 Recovery / flatness path — the defect (§4)

`backend/api/services/runtime_recovery.py:58-77` computes `is_flat = not remaining_legs and not broker_positions` (`:69`), where `broker_positions` comes from `_live_broker_positions_for_attribution` (`backend/api/routers/worker_shared.py:487-550`). That helper matches **orders** by attribution refs (`:501-505`) but then returns the **entire account position** for every touched `(instrument_token, product, exchange, tradingsymbol)` key (`:519`, `:533-548`, quantity at `:545`) with no subtraction of another strategy's book. The same coarse attribution appears in `_list_live_strategy_broker_positions_sync` (`backend/api/repositories/algo_worker_repo.py:1448-1505`), which attributes *instruments* by order and returns full account rows (`:1492-1499`).

### 2.6 Paper runtime

- Durable per-strategy state derived by replay: `backend/paper_runtime/run_state.py:69` (`get_run_state`), identity resolution over `strategy_run_id|option_strategy_id|strategy_id|algo_instance_id` (`:45`, `:79-80`).
- Settlement read model: `backend/paper_runtime/service.py:509` (`get_strategy_run_settlement_readonly`).
- Per-lot attribution: `paper_position_lots` (`backend/schema.sql:880`), writer at `backend/paper_runtime/repository.py:888`, reader at `:1106`.
- **No partial fills**: `"filled_quantity": order.quantity` (`backend/paper_runtime/service.py:991-1000`). `PARTIALLY_FILLED` is referenced only in pending-status queries.
- Admission: lot-multiple check `backend/paper_runtime/service.py:131-133`; account-scoped funds/margin check `:135-149`. Slippage = level-1 opposite depth (`_fill_price` `:1790-1812`).

### 2.7 Options

- Run state machine `created → entry_previewed → entering → entered | partial_entry → cleanup_required → exit_previewed → exiting → partial_exit | exited`: `backend/options/execution/models.py:12-22`; transitions `backend/options/execution/lifecycle.py:8-40`; durable store `option_run_states` (`backend/schema.sql:1212`).
- Ordering is buy-first, not role-aware: `backend/options/execution/planner.py:6-15`; planner consumes raw `quantity` (`:29-31`) — `lots`/`lot_size` are advisory after run creation.
- Hosted guards: paper-only mutation (`backend/options/api/worker_options_router.py:80-87`, `:220-227`), execution-injection forbidden (`:93-106`), safety token enforced on enter/exit (`:256-257`, `:292-293`).
- Identity: the option run reuses the worker run id as its own key (`backend/options/execution/durable_store.py:94-103`) — an identity overload corrected in §5.3.
- Protection evaluates run state but is **recommendation only**: `backend/options/protection/runtime.py:73-116` builds exit orders; nothing submits them; the only live effect is blocking new entries via the safety check (`backend/api/routers/worker_protection.py:639`, `:866-867`).

### 2.8 Risk and protection enforcement surface

- Generic protection is the only enforced runtime risk layer: `evaluate_backend_protection` (`backend/api/services/protection.py:388-607`), rules `position_stoploss:476`, `position_target:486`, `position_trailing_stoploss:498`, `worker_stale:511`, `mis_squareoff_buffer:524-539`, `basket_*:551/563/575`; **all actions are `exit_strategy`** (`_action_for_rule:365-367`).
- Cadence: background loop, `WORKER_PROTECTION_INTERVAL_SECONDS` default 5 (`backend/app/background.py:40-73`, interval at `:50`).
- No daily-loss, notional, quantity, order-count or max-concurrent-position cap exists anywhere. `allocation_cap` is reporting-only (`backend/api/routers/worker_protection.py:209-222`, consumed only at `:245-266`); the funds snapshot hands a run the whole account's cash (`:170`, `:190`).
- Live margin is quoted on demand only: `backend/broker_api/orders/service.py:597-609`, `:611-624`.

### 2.9 Instrument catalog

`InstrumentDescriptor` carries `lot_size`, `tick_size`, `expiry`, `strike`, `underlying`, `option_type`, `catalog_generation`, `lifecycle_status` (`backend/broker_api/instruments/catalog.py:136-162`). **No freeze quantity.** Reads always resolve the *current* generation (`:750` view; `backend/schema.sql:192-226`) — there is no as-of-generation query and no pin on orders/positions. No tick rounding and no freeze slicing exist (grep for `freeze` in `backend/` returns nothing; `autoslice` is a passthrough at `backend/broker_api/orders/models.py:61`).

---

## 3. Ownership model (replaces exclusive claims)

### 3.1 SUPERSEDED

The earlier recommendation — *exclusive `(account, instrument, product)` claims, deny overlap* — is superseded. Rationale: the platform already records `strategy_run_id`, `client_order_ref`, broker order id, order→run links, linked trade fills, per-run attributed net quantity, and exits generated from that quantity (§2.3, §2.4). The correct model is **fill attribution with per-strategy settlement**, not ownership locking.

### 3.2 The V1 ownership rule

1. Multiple strategies may own the same canonical instrument.
2. Ownership is represented by **attributed fills and the resulting per-strategy quantity**.
3. Each strategy exits only its own attributed net quantity (`worker_execution.py:231-251`).
4. The broker continues to expose one account-level net position per instrument/product.
5. Account reconciliation verifies:

   ```
   sum(strategy-attributed quantities)
   + identified manual / unattributed quantity
   = broker account quantity
   ```

6. A strategy can be **exposure-settled** while another strategy continues holding the same instrument.
7. Broker account net zero is **never** required to close one strategy.
8. No permanent exclusive claim is introduced as the default model.

The aggregate identity in (5) already has an implementation embryo: the control-plane snapshot subtracts attributed quantities from broker net and labels the residual "Manual / unattributed broker exposure" (`backend/api/services/control_plane.py:330-393`, subtraction at `:342-359`).

### 3.3 Required real-world case (accepted)

```
Momentum Portfolio A:  RELIANCE, product CNC, quantity +100
Mean Reversion B:      RELIANCE, product MIS, quantity +20 or -20 (intraday)

Broker: one combined account position, separated by product
        where the broker provides that distinction
```

Both strategies operate independently; each exit is computed from its own attributed fills.

MIS rules for V1:

- treat MIS as intraday;
- require a strategy/platform exit deadline **before** broker cutoff — the `mis_squareoff_buffer` rule already exists for this purpose (`backend/api/services/protection.py:524-539`) and the schedule model carries `squareoff_at` (`backend/strategies/models.py:137`);
- do **not** rely on broker auto-square-off;
- preserve attribution through partial fills, rejects and late events;
- if the position cannot close, enter `action_required` and continue reconciliation;
- an MIS short is not a multi-day short strategy; multi-day short exposure belongs to futures, options, or a later explicitly-designed SLB path.

Product separation is structural in the platform: positions and fills are keyed with `product` (`backend/schema.sql:658`, `order_trade_fills` join on product at `backend/api/repositories/algo_worker_repo.py:1265-1270`).

### 3.4 V1 same-product overlap boundary (locked)

- **Allowed:** same-direction overlap in the same instrument and product; CNC and MIS are separately attributed products.
- **Deferred from Product V1:** opposing virtual positions in the *exact same* instrument and product, unless current code and broker evidence prove safe independent settlement.
- **Not in V1:** account-level netting allocator; internal crossing of one strategy's buy against another strategy's sell. Each strategy's broker orders are submitted and attributed independently.

**Required supported example**

```
A RELIANCE CNC +100        B RELIANCE CNC +20        broker RELIANCE CNC +120

B exits 20  ->  B = 0,  A = +100,  broker = +100
B may be declared exposure-settled after its own work is quiescent (§6).
```

**Deferred example and why**

```
A RELIANCE CNC +100        B RELIANCE CNC -20        broker RELIANCE CNC +80
```

Current validation (`worker_execution.py:208-229`) requires, for A's long, `broker_net >= attributed_net` — here `80 < 100`, so A's exit is refused (409). That refusal is *correct behaviour for today's evidence*, because from the platform's position the number `80` is indistinguishable between three causes:

1. B genuinely holds an opposing virtual short of 20 (safe to unwind);
2. a manual/out-of-band sale of 20 occurred (§2.2 absent manual book);
3. attribution is incomplete — a late fill, unresolved link, or unsynced order (`backend/api/repositories/algo_worker_repo.py` legacy fallback paths).

Only (1) is safe to act on. If the platform assumed (1) and the truth was (2), exiting A's full +100 would drive the account to −20, creating an **unowned naked short**. Distinguishing (1) from (2)/(3) requires the aggregate reconciliation identity of §3.2(5) plus settlement/quiescence evidence (§6) — which is why opposing same-product exposure is deferred rather than guessed. This deferral is a *proof* deferral, not a capability gap in the broker.

---

## 4. Current closure/recovery defect (narrow, evidenced)

### 4.1 Already working

- Per-run attributed net: `backend/api/repositories/algo_worker_repo.py:1212-1277`.
- Direct exit sizing and validation from attributed quantity: `backend/api/routers/worker_execution.py:231-251`, `:208-229`.
- Same-direction overlap therefore already survives both entry and exit — the broker net being *larger* than the attributed net passes validation.

### 4.2 Inconsistent: recovery and flatness use the account position, not the strategy book

- `_live_broker_positions_for_attribution` matches orders by attribution refs but returns the full account position for each touched key: `backend/api/routers/worker_shared.py:501-505`, `:533-548`.
- `_list_live_strategy_broker_positions_sync` attributes instruments by order then returns full account rows: `backend/api/repositories/algo_worker_repo.py:1448-1505`.
- Consumption sites: `is_flat = not remaining_legs and not broker_positions` (`backend/api/services/runtime_recovery.py:69`), and the deferred branch of live exit (`backend/api/routers/worker_execution.py:285-307`).

**Consequence:** when two strategies hold the same instrument/product, the second strategy's remaining quantity keeps `broker_positions` non-empty, so an already-flat run is reported `deferred` / `stalled` and cannot be finalized — even though its own attributed quantity is zero and its work is terminal.

### 4.3 Required correction

Replace "broker position exists for a touched instrument" with:

```
flat(run) := attributed_net(run) == 0
             AND no open/pending orders for the run
             AND no unresolved links/fills for the run
             AND baskets/brackets/plans for the run terminal
             AND no outstanding execution authority
```

and evaluate the account-level identity `sum(attributed) + unattributed = broker` **separately** as an account reconciliation check, not as a per-run flatness predicate. This is a *narrowing* of existing logic, not a new engine: the exact attributed query already exists (`algo_worker_repo.py:1212-1277`) and needs to be the one used by flatness paths.

---

## 5. Core R1 architecture

### 5.1 One Strategy product, two compute adapters

**Hosted Python**

- source and version stored by the platform; supervisor starts and contains the child;
- Run now plus platform schedules;
- restricted capabilities and environment;
- logs and process lifecycle managed by the platform.
- Current state: **Implemented** for manual run (source storage, versioning, supervisor, child token, run binding, session nonce, paper execution); **Missing** for scheduling (report `:576-583`; `backend/strategies/__init__.py:14`).

**External Algo Worker**

- process deployed and supervised by the user;
- may use custom dependencies, GPUs, external stores and networking;
- attaches through the worker protocol (`sdk/python/kite_algo_worker/client.py:83-96`, `:1074-1078`);
- retains compatible advanced APIs.
- Current state: **Implemented**.

Both feed the same shared execution platform. **No second order engine, ledger, risk engine or protection engine.**

### 5.2 Durable strategy, disposable attempts

The durable strategy owns: versions, configuration, capital allocation, schedules, targets, attributed positions, plans, P&L, protection, checkpoints, journal, reconciliation history. A job/run/attempt executes **on behalf of** it.

Current state: **Partial.** Versions (`hosted_strategy_versions`), strategy config, jobs with `attempt`/`lease_epoch`, and runs exist (`backend/schema.sql:2286`, `:2371`, `:953`); capital, targets, attributed position (as durable state), plans and checkpoints do not.

A process restart must not create a new economic owner. Current recovery already fences rather than resumes (`runtime_recovery.py:167-331`), which satisfies "no new economic owner".

### 5.3 Proposal contract

**PROPOSED.** Hosted children submit durable typed proposals:

`target_weights`, `target_position`, `target_futures`, `target_option_structure`, `intent_bundle`

and may request bounded actions:

`cancel_pending`, `reduce_position`, `exit_position`, `exit_structure`, `exit_strategy`

Hosted live children do not receive unrestricted raw broker-order authority. Server-side denial is authoritative; removing SDK methods is insufficient — a child could call HTTP directly.

Every proposal is persisted **before compilation** and carries separate ids:

`strategy_id`, `version_id`, `job_id`, `attempt`, `evaluation_id`, `proposal_id`, `proposal_revision`, `execution_plan_id`, `option_run_id` (where applicable), `account_scope`, `execution_mode`.

Current state: **Missing.** Today the child emits intents/baskets/brackets/option-run calls directly (§2.2), and option runs overload the worker run id as their own primary key (`backend/options/execution/durable_store.py:94-103`). The intent dedupe key `(strategy_run_id, idempotency_key)` (`backend/schema.sql:1013`) is the seed for proposal idempotency.

### 5.4 Immutable plan

Semantic proposal units: weight fractions, shares, lots, structure units, leg ratios, bounded action quantities.

The compiler resolves them once against: strategy capital; strategy-attributed position; pending and unsettled work; market/account snapshot; **pinned catalog generation**.

Store both logical and execution forms (`lots` + `lot_size` + `quantity_units`; `structure_units` + `ratio` + `quantity_units`). Do not reinterpret a frozen plan after catalog changes. **Only relevant changes to a pinned instrument invalidate a plan; an unrelated catalog-generation change does not.**

Current state: **Missing** (no plan entity; catalog unpinnable, §2.9).

### 5.5 Settlement

Three levels are retained:

- **WORK_SETTLED** — the submitted execution work is terminal and completely accounted for; exposure may remain.
- **TRANSITION_SETTLED** — a specific transition (roll or adjustment) completed; only the claims/portions it made obsolete are released.
- **EXPOSURE_SETTLED** — this strategy's attributed exposure is zero and its work is quiescent.

For shared instruments, exposure settlement requires:

- strategy attributed quantity zero;
- strategy open/pending orders zero;
- strategy unresolved fills zero;
- strategy baskets/brackets/protection terminal;
- no remaining strategy execution authority;
- account aggregate reconciliation valid (§3.2(5)).

It does **not** require broker account quantity zero when another strategy owns the remainder.

A durable execution/quiescence **version** must be designed so that two matching reads or a quiet-time delay is not treated as proof. Current state: **Partial** — phase-1 guards plus phase-2 flatness exist (`backend/api/services/runtime_recovery.py:80-144`), but quiescence is hardcoded `unverified` with exactly this rationale documented (`backend/strategies/reconciliation.py:103-106`), and the fail-closed assess/commit discipline already exists (`evidence_digest` `:112-137`).

### 5.6 Capital and reservations

Product V1 uses **fixed-INR per-strategy allocation** with basis recorded.

Admission accounts for: attributed exposure; in-flight orders; unsettled fills; reserved capital/margin; the proposed plan. Reservations must be **durable and atomic** so concurrent plans cannot both spend the same capacity. Default over-limit behaviour is a **named refusal**; deterministic resizing is permitted only when explicitly configured, previewed and persisted, and must never happen silently.

Current state: **Missing** — allocation is reporting-only (`backend/api/routers/worker_protection.py:209-222`, `:245-266`); the only real admission is paper's account-scoped funds check (`backend/paper_runtime/service.py:135-149`).

### 5.7 Risk and protection

One layered model, most restrictive wins:

```
platform guardrails → account profile → strategy-version policy → proposal policy
```

One shared protection action vocabulary:

`alarm`, `cancel_pending`, `freeze_new_risk`, `reduce_position`, `exit_strategy`, `flatten_structure`, `account emergency action`

Migrate generic protection and options protection into one authorization/execution service. Domain engines may calculate evidence but must not invent separate action lifecycles.

Current state: **Partial/Incompatible.** Generic protection is enforced but single-action (`exit_strategy` only — `backend/api/services/protection.py:365-367`) and percent-based; options protection is recommendation-only (§2.7); there is no `freeze_new_risk` and no `reduce_position`.

### 5.8 Scheduling

Product V1: Run now; daily; weekly; **monthly trading-calendar** schedules; finite evaluation jobs; explicit misfire policy; explicit overlap policy; versioned checkpoints; enable/disable; occurrence history.

Job type follows the **evaluation model**, not asset class. Continuous jobs are for genuinely streaming/custom strategies. Finite jobs are the normal model for monthly portfolios, daily swing evaluations, scheduled futures rolls and scheduled option entries.

Current state: **Partial.** The schedule table exists but is stored-only, daily/weekly only, paper/dry-run only (`backend/strategies/models.py:115-181`); `occurrence_key` idempotency exists (`:252`); `manual_paused_at` exists (`:139`). No runner, no monthly kind, no misfire/overlap policy.

### 5.9 Futures

Documented model: semantic lots; contract-selection policy; immutable resolved contracts; tick/lot/freeze metadata; live margin precheck; **one parent roll operation with child plans**; sequencing policies — acquire-first, reduce-first, paired-chunk, native-spread; **no automatic financial rollback** (rollback may undo database preparation before submission, never a broker fill); the source-contract portion's attribution released only for the strategy portion proven transitioned.

Current state: **Missing.** `resolve_futures_contract` is PROPOSED (`documents/hosted-strategies-design.md:215-217`) and deferred (report `:584-587`); no rollover code exists; no freeze quantity; no tick rounding; no live margin precheck (§2.9, §2.8).

### 5.10 Options

Documented model:

- `target_option_structure`;
- frozen chain snapshot and resolved legs;
- semantic leg roles and ratios;
- the **existing option run is the execution coordinator** — no second options engine;
- a hedge fill releases only the matching short quantity;
- structure-level margin and maximum loss;
- enforced protection (not advisory);
- the child cannot directly create/enter/exit target-controlled option runs;
- **stopping the child is not exiting the structure**;
- strategy-scoped settlement even when another strategy uses the same contract.

Current state: **Partial/Incompatible.** Engine, state machine and store are implemented (§2.7); hosted options is paper-only by design; ordering is buy-first rather than role-based; protection never fires; identity is overloaded; there is no structure-level intent record.

### 5.11 Corporate actions

Product V1 begins with: **detection; classification; plan/protection invalidation; freeze affected new risk; operator evidence and escalation.**

Corporate actions create **immutable attribution adjustments, never fabricated trades**. For splits and bonuses, allocate the change across each strategy's **pre-action attributed quantity**.

Current state: **Missing.** Corporate actions appear only in fundamentals and mutual funds; a split would be absorbed silently by position reconcile (`backend/broker_api/orders/order_runtime.py:865`) while attributed quantity stays stale — which can wedge the exit validation (§3.4) or corrupt attributed P&L.

### 5.12 Paper realism

Paper mode must support **deterministic partial fills** sufficient to test: partial portfolio rebalance; partial futures roll; option hedge filled but short leg rejected; capital reservation release; exit and settlement; cleanup/recovery. Not a full exchange queue simulator.

Current state: **Missing** — whole-quantity fills only (`backend/paper_runtime/service.py:991-1000`).

### 5.13 UI principles

The user sees one **Strategy** concept with two ways to start it: "Run on this platform" or "Connect external worker". The strategy surface shows: capital, account, mode, schedule, current target, strategy-attributed positions, broker account aggregate, plan and order progress, P&L, protection, notifications, **Stop evaluator**, **Exit position/strategy**, reconciliation.

Internal claim/reservation mechanics are not exposed in the ordinary workflow. The UI must distinguish explicitly between: stopping code; disabling a schedule; cancelling pending work; exiting positions; reconciliation. ("Stop evaluator" is not "Exit position".)

---

## 6. Momentum Portfolio walkthrough (complete example)

> Status markers per artifact: **[implemented]** = works today; **[proposed]** = R1 design, not built.

### 6.1 Strategy definition

```yaml
# PROPOSED configuration payload (strategy version + runtime config)
strategy_id:        stg_momentum_portfolio
version_id:          hsv_7                  # [implemented] hosted_strategy_versions
name:               Momentum Portfolio
account_scope:      kite:AB1234             # [implemented] account_scope allowlist
execution_mode:     paper                   # [implemented] paper | dry_run
capital:                                    # [proposed] §5.6
  basis: fixed_inr
  amount: 1000000
cash_buffer_pct:    10
universe:           NIFTY100
ranking:            momentum_12_1           # 12-month return minus most recent month
selection:          top_5
target_semantics:   full_snapshot           # every evaluation states the whole book
drift_band_pct:     2
product:            CNC
```

```yaml
# PROPOSED schedule payload (extends hosted_strategy_schedules, currently stored-only)
schedule:
  kind: monthly_trading_calendar            # today only daily|weekly exist
  day: first_trading_day
  at_time: "09:20"
  timezone: Asia/Kolkata
  job_kind: finite
  misfire_policy: run_once_if_within_window # proposed
  overlap_policy: skip_if_active            # proposed
  max_duration_s: 900
  progress_deadline_s: 300
```

```python
# PROPOSED hosted Python file (illustrative)
from kite_algo_worker import hosted

def main(ctx):
    # ctx.client / ctx.run are attach-only [implemented] hosted.attach_run
    universe = ctx.client.universes.resolve("NIFTY100")            # [implemented]
    prices   = ctx.client.marketdata.historical(universe, days=380) # [implemented]
    weights  = momentum_12_1_top5(prices)                           # strategy logic

    ctx.run.propose(                                                # [proposed] §5.3
        proposal_type="target_weights",
        evaluation_id=ctx.evaluation_id,          # 2026-10-01#monthly
        payload={
            "semantics": "full_snapshot",         # absolute targets, not deltas
            "cash_buffer_pct": 10,
            "weights": {                          # weights of allocated capital
                "NSE:RELIANCE": 0.20,
                "NSE:TCS":      0.15,
                "NSE:INFY":     0.15,
                "NSE:HDFCBANK": 0.12,
                "NSE:ICICIBANK":0.10,
            },
        },
        execution_policy={
            "order_type": "LIMIT",
            "price_policy": "limit_at_ltp_plus_slippage",
            "max_slippage_pct": 0.25,
            "drift_band_pct": 2,
            "sequencing": "sells_before_buys",
        },
    )
    ctx.run.log_decision_event("proposal_submitted")                 # [implemented]
    # The child may exit after the proposal is accepted and persisted.
    # Platform continues execution. [proposed: acceptance handoff]
```

### 6.2 Emitted proposal (PROPOSED shape)

```json
{
  "proposal_id": "prp_01J8...",
  "proposal_revision": 1,
  "proposal_type": "target_weights",
  "evaluation_id": "2026-10-01#monthly",
  "strategy_id": "stg_momentum_portfolio",
  "version_id": "hsv_7",
  "job_id": "job_01J8...",
  "attempt": 1,
  "account_scope": "kite:AB1234",
  "execution_mode": "paper",
  "payload": {
    "semantics": "full_snapshot",
    "cash_buffer_pct": 10,
    "weights": {
      "NSE:RELIANCE": 0.20, "NSE:TCS": 0.15, "NSE:INFY": 0.15,
      "NSE:HDFCBANK": 0.12, "NSE:ICICIBANK": 0.10
    }
  },
  "execution_policy": {
    "order_type": "LIMIT",
    "max_slippage_pct": 0.25,
    "drift_band_pct": 2,
    "sequencing": "sells_before_buys"
  }
}
```

### 6.3 Compiled plan (PROPOSED shape)

```json
{
  "execution_plan_id": "pln_01J8...",
  "proposal_id": "prp_01J8...",
  "proposal_revision": 1,
  "catalog_generation": "gen_2026-09-30",
  "compiled_at": "2026-10-01T09:21:04+05:30",
  "capital": {"basis": "fixed_inr", "amount": 1000000, "reserved": 986000},
  "effective_exposure": {
    "attributed": 412000, "in_flight": 18000, "reserved_margin": 0
  },
  "steps": [
    {"seq": 1, "action": "sell", "instrument_id": "NSE:EQ:WIPRO", "quantity_units": 300,
     "logical": {"reason": "not_in_target"}, "claim": "attributed_only"},
    {"seq": 2, "action": "buy", "instrument_id": "NSE:EQ:RELIANCE",
     "quantity_units": 68, "logical": {"target_weight": 0.20, "lot_size": 1},
     "limit_price": 2894.50, "catalog_generation": "gen_2026-09-30"}
  ],
  "warnings": ["cash_constrained: scaled to fit cash buffer"]
}
```

Walkthrough behaviour:

1. **Fixed ₹10 lakh allocation** — enforced at admission against attributed + in-flight + reserved + plan (§5.6).
2. **Monthly first-trading-day schedule** — one finite evaluation job per month; occurrence key makes a retry idempotent.
3. **12-1 momentum ranking, top-5 weights** — computed by the strategy, expressed as absolute weights.
4. **10% cash buffer** — part of the compiler's cash constraint, not the strategy's arithmetic.
5. **Full-snapshot target semantics** — anything not named is targeted to zero; deltas are never implied.
6. **Drift band** — a plan is produced only when `|target − attributed| / allocated > band`; otherwise the evaluation records "no action" and exits.
7. **Platform quantity calculation** — weight → rupees → shares, largest-remainder rounding to one-share increments, and to lot multiples where the instrument requires them.
8. **Sells before buys** — cash from sells is sequenced before buys so the plan does not need temporary margin.
9. **Child exits after proposal acceptance** — the proposal is durable; execution does not depend on the child staying alive.
10. **Positions persist to the next month** — attributed positions are durable state, not process memory.
11. **Concurrent mean-reversion strategy** — may own RELIANCE simultaneously under §3.4; same-direction CNC overlap is supported, and each strategy's settlement is independent.

---

## 7. Current parity

Legend: **Implemented** · **Partial** · **Missing** · **Incompatible** · **Unsafe**

### A. Momentum equity portfolio

| Requirement | Status | Evidence / consequence |
|---|---|---|
| Durable strategy identity across attempts | Partial | `hosted_strategy_versions` + `strategy_jobs.attempt` exist (`schema.sql:2286`, `:2371`); no capital/target/attributed-position state |
| Target weights + portfolio compiler | Missing | no target endpoint (`endpoint_manifest.py:53-95`); only dead ORM (`broker_api.py:192-204`) |
| Capital allocation enforcement | Missing | `worker_protection.py:209-222` reporting-only; paper is account-scoped (`paper_runtime/service.py:135-149`) |
| Drift-band rebalance | Missing | no diff engine anywhere |
| Deterministic rounding / lot constraints | Partial | paper checks lot multiples (`paper_runtime/service.py:131-133`); no compiler, no tick rounding |
| Insufficient-cash behaviour | Partial | paper rejects at account level (`:148-149`); live has no local check; no reservation model |
| Months-long attributed P&L | Partial | paper exact (`paper_runtime/run_state.py:69`); live is a read model |
| Scheduled monthly evaluation | Missing | schedule stored-only, daily/weekly only (`strategies/models.py:115-181`); no runner (`strategies/__init__.py:14`) |
| Corporate-action awareness | Missing | present only in fundamentals/mutual funds; reconcile would absorb silently (`order_runtime.py:865`) |
| Partial fills in paper | Missing | whole-quantity fills (`paper_runtime/service.py:991-1000`) |
| Settlement of one strategy while others hold | Missing | §4.2 defect blocks closure while another strategy holds the instrument |

### B. Single-stock mean reversion

| Requirement | Status | Evidence / consequence |
|---|---|---|
| Target position API | Missing | §2.2 |
| Own stop / max-loss policy | Partial | generic protection only, single action `exit_strategy` (`api/services/protection.py:365-367`) |
| Add/reduce over days | Partial | intents support it; no plan, no reservations |
| MIS intraday deadline | Partial | `mis_squareoff_buffer` rule exists (`protection.py:524-539`); `squareoff_at` stored (`strategies/models.py:137`) |
| Same-direction overlap with A | Implemented | attributed leg query `algo_worker_repo.py:1212-1277`; exit sizing `worker_execution.py:231-251`; validation tolerates larger broker net `:220-229` |
| Opposing same-product overlap | Incompatible by design | refused at validation `:220-229`; rationale §3.4 |
| Per-strategy attribution in paper | Implemented | `paper_runtime/run_state.py:69-287`; lots `schema.sql:880` |
| Per-strategy attribution in live | Partial | derived read model only; no durable ledger |

### C. NIFTY futures and rollover

| Requirement | Status | Evidence / consequence |
|---|---|---|
| Contract selection (`resolve_futures_contract`) | Missing | PROPOSED `design.md:215-217`; deferred report `:584-587` |
| Lot semantics at execution | Incompatible | planner uses raw `quantity`; lots advisory (`options/execution/planner.py:29-31`) |
| Freeze quantity | Missing | no `freeze` field anywhere; `autoslice` passthrough (`orders/models.py:61`) |
| Tick rounding | Missing | `tick_size` never applied to prices |
| Live margin precheck | Missing | broker-quoted on demand (`broker_api/orders/service.py:597-609`) |
| Rollover lifecycle | Missing | no roll code; no ordering policies |
| Catalog pinning | Missing | always current generation (`instruments/catalog.py:750`) |

### D. BANKNIFTY option structure

| Requirement | Status | Evidence / consequence |
|---|---|---|
| Chain/expiry/strike resolution | Implemented | `options/market/service.py:115-240` |
| Run lifecycle + durable store | Implemented | `options/execution/models.py:12-22`; `schema.sql:1212` |
| Leg roles and ratios | Missing | legs have no `role`; ordering is buy-first (`planner.py:6-15`) |
| Hedge-first by role | Partial | buy-first approximation only |
| Partial fill / reject basket state | Implemented | option-run leg buckets + basket legs (`schema.sql:1019`, `:1042`); duplicated representations |
| Structure-level max loss / margin | Partial | options protection metrics exist but never fire (`options/protection/runtime.py:73-116`) |
| Hosted live options | Unsafe | blocked by design today: `HOSTED_OPTIONS_MUTATION_PAPER_ONLY` (`worker_options_router.py:80-87`, `:220-227`) |
| Identity separation (run vs option run) | Incompatible | option-run PK reuses worker run id (`options/execution/durable_store.py:94-103`) |
| Child cannot inject fills | Implemented | `HOSTED_EXECUTION_INJECTION_FORBIDDEN` (`worker_options_router.py:93-106`) |

### E. Multiple strategies in one broker account

| Requirement | Status | Evidence / consequence |
|---|---|---|
| Order → strategy attribution | Implemented | `live_order_intents` (`schema.sql:1363`), links (`:1402`) |
| Per-strategy attributed quantity (live) | Partial | derived SQL (`algo_worker_repo.py:1212-1277`) |
| Per-strategy attributed quantity (paper) | Implemented | `paper_runtime/run_state.py:69` |
| Strategy-scoped exit | Implemented | `worker_execution.py:231-251` |
| Strategy-scoped flatness/closure | Unsafe | defect: account position used for a touched instrument (`worker_shared.py:533-548` + `runtime_recovery.py:69`) |
| Aggregate account reconciliation | Partial | control-plane subtraction + unattributed bucket (`control_plane.py:330-393`) |
| Durable capital/risk reservations | Missing | §5.6 |
| Unified protection engine | Missing | three rule systems today (§2.7, §2.8) |
| Settlement barrier with quiescence version | Partial | guards + flatness exist; quiescence hardcoded unverified (`strategies/reconciliation.py:103-106`) |
| Manual/unattributed book | Partial | unattributed bucket only; no manual book of record |

**Parity counts (45 requirement rows above):** Implemented 9 · Partial 14 · Missing 17 · Incompatible 3 · Unsafe 2.

---

## 8. R1 implementation dependency map

High-level dependency order (not a task plan):

1. Correct and certify fill attribution and per-strategy exit/settlement (§4.3).
2. Add aggregate account reconciliation across strategy books (§3.2(5)).
3. Establish durable strategy identity across attempts (§5.2).
4. Add durable capital/risk reservations (§5.6).
5. Add proposal persistence and the immutable plan model (§5.3, §5.4).
6. Implement `target_position` and `intent_bundle` first.
7. Implement `target_weights` and the portfolio compiler.
8. Add the trading-calendar scheduler and checkpoints (§5.8).
9. Add deterministic paper partial fills (§5.12).
10. Add the futures resolver and roll orchestration (§5.9).
11. Bind `target_option_structure` to the existing option-run engine (§5.10).
12. Add corporate-action detection (§5.11).
13. Stage paper and live certification by scenario.

Dependency notes: (1) and (2) are prerequisites for every closure decision; (4) must precede (5)–(7) or admission will be built twice; (9) gates credible paper validation of (6), (7), (10) and (11).

---

## 9. R2 Grill backlog

Deferred by design. Recommended starting positions are recorded, and each entry states why it stays open.

1. **Manual Kite trades and the manual/unattributed account book.** *Starting position:* treat the residual `broker − sum(attributed)` as an explicit manual book with operator annotation; never auto-assign it to a strategy. *Open because:* today it is only a display bucket (`control_plane.py:362-393`), and its arithmetic consequences for exits are not defined.
2. **Opposing virtual positions in the same instrument/product.** *Starting position:* remain deferred until aggregate reconciliation plus quiescence evidence can distinguish "another strategy's opposite book" from "manual trade" and "late fill" (§3.4). *Open because:* acting on the wrong assumption creates unowned naked exposure.
3. **Whether any strategy may request exclusive ownership.** *Starting position:* available only as an explicitly configured, operator-visible mode with its own admission and release semantics. *Open because:* exclusivity is a product preference, not an accounting necessity.
4. **Eventual account-level order netting.** *Starting position:* not in V1; revisit only if crossing is proven safe and auditable. *Open because:* it contradicts independent attribution and changes the audit model.
5. **Capital reallocation while positions remain open.** *Starting position:* reallocation is a new versioned capital record; existing plans and reservations are not reinterpreted. *Open because:* it interacts with admission and with in-flight work.
6. **Live proposal auto-execution versus operator approval.** *Starting position:* auto-execute only inside the pinned policy envelope, with an operator approval mode as a strategy setting. *Open because:* it is a risk-appetite decision, not an architecture one.
7. **Conflicting protection actions between strategies.** *Starting position:* protection acts only on the owning strategy's attributed exposure; account-level emergency actions are separate and audited. *Open because:* partial-fill and shared-instrument interactions are not yet modelled.
8. **Shared option contracts across structures.** *Starting position:* allow same-direction sharing; treat opposing structures on the same contract like §3.2. *Open because:* option settlement (exercise/expiry) changes the proof requirements.
9. **Assignment/exercise and expiry settlement.** *Starting position:* platform-driven risk-reducing action before expiry; never rely on broker defaults. *Open because:* STT/exercise mechanics and attribution across a physical settlement are unmodelled.
10. **Merger/demerger attribution.** *Starting position:* successor-based adjustment records (§5.11); automation later. *Open because:* instrument identity changes, unlike splits.
11. **External-worker compatibility migration.** *Starting position:* capability-versioned API; external workers keep raw APIs but remain subject to claims/admission on shared live accounts. *Open because:* migration sequencing affects existing users.
12. **Account-level emergency controls.** *Starting position:* one audited "account emergency action" distinct from strategy protection. *Open because:* authority and blast radius need an explicit policy.
13. **Fee and tax attribution.** *Starting position:* per-strategy attribution of charges from execution facts. *Open because:* existing journal facts carry fees per fill (`schema.sql:1749+`) but no per-strategy allocation policy exists.
14. **Late broker corrections after strategy settlement.** *Starting position:* a settled strategy can receive a post-settlement adjustment record; it does not silently reopen. *Open because:* it defines what "settled" means operationally.

---

## 10. Corrections ledger (R1 vs the earlier consolidated report)

| # | Earlier position | R1 position |
|---|---|---|
| 1 | Exclusive `(account, instrument, product)` claims, deny overlap | **Replaced** by shared fill attribution with per-strategy settlement (§3.2) |
| 2 | Overlap between strategies to be blocked | **Same-direction overlap accepted**, including CNC/MIS product separation (§3.4) |
| 3 | Product treated as one scope | **CNC and MIS are separately attributed products** (§3.3, §3.4) |
| 4 | Not addressed | **Same-product opposing exposure deferred from V1** with an explicit proof argument (§3.4) |
| 5 | Settlement defined as broker flat for touched instruments | **Strategy-flat (attributed = 0 + quiescent) plus aggregate account reconciliation**; broker net zero not required (§5.5) |
| 6 | Recovery defect described generally | **Narrowed to two evidenced call sites** (`worker_shared.py:487-550`, `algo_worker_repo.py:1448-1505` consumed at `runtime_recovery.py:69` and `worker_execution.py:285-307`) (§4) |
| 7 | Operator release of a claim | **Operator cannot force a financially unproven release**; release requires the appropriate settlement level (§5.5) |
| 8 | Deterministic resizing discussed loosely | **Silent resizing rejected**; resizing only when configured, previewed and persisted (§5.6) |
| 9 | Catalog changes invalidate pinned work broadly | **Only relevant changes to a pinned instrument invalidate a plan**; unrelated generation changes do not (§5.4) |
| 10 | Not addressed | **Hosted child has no unscoped account-wide flatten**; exits are strategy-scoped, account-level action is a separate audited control (§5.7, §5.13) |

---

## 11. Verification note

- All `path:line` citations refer to the state of the repository at the commit that adds this document.
- Claims marked Implemented were read directly during this revision. Claims marked Partial/Missing/Incompatible/Unsafe are supported either by a positive citation of the deficient behaviour or by targeted searches that returned nothing (e.g. `freeze` across `backend/`).
- Where this document and `documents/hosted-strategies-design.md` disagree, **this document records the R1 decision and the disagreement is listed in §1.4**; the earlier document remains as historical input.
