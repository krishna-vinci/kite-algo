# Hosted and External Strategy Architecture — Draft R2

**Status:** Design draft under review
**Product target:** Hosted Strategies Product V1
**Architecture revision:** R2 (answers the R1 §9 backlog; R1 remains unchanged as the revision record)
**Date:** 2026-09-16
**Supersedes:** nothing. R2 answers, refines and where necessary corrects R1; conflicting points are listed in §5.
**Follow-on:** R3 if the escalations in §4 change any locked boundary.

**Reading rule.** Every answer below is either **SETTLED** (research decides it; implement as described unless someone objects with evidence) or **ESCALATION** (a product/risk decision that research cannot make; recorded in §4 with options and a recommendation). No escalation is silently resolved here.

---

## 0. Revision scope

R1 left 14 product questions open. R2 answers all 14, splits them into settled vs escalation, and records three cross-cutting findings that were discovered while answering them (§2) — including one live misattribution path and one missing observation channel that several answers depend on.

---

## 1. Answers to the R1 backlog

### Q1. Manual Kite trades and the manual/unattributed account book

**Question.** What is the manual book, and how does it relate to strategy books?

**Research findings.**

- Positions are already observed account-wide: `reconcile_account_positions` writes every broker net position into `account_positions` (`backend/broker_api/orders/order_runtime.py:865`), regardless of origin.
- Orders are *fetchable* account-wide: `OrdersService.orders()` calls `kite.orders()` (`backend/broker_api/orders/service.py:440-444`) and is already used to filter for attribution (`backend/api/routers/worker_shared.py:500-505`).
- Orders are **not persisted** account-wide: the sync loop only processes orders the platform tracks (`dirty_for_trade_sync = TRUE OR needs_reconcile = TRUE`, `order_runtime.py:525`), and fills are only stored for those (`:578`).
- The manual book therefore exists today only as a display residual: `_unattributed_bucket` subtracts attributed quantities from broker net and labels the remainder "Manual / unattributed broker exposure" (`backend/api/services/control_plane.py:362-393`, subtraction at `:342-359`).
- Journal-level, unattributed fills are labelled `broker_import` (`backend/schema.sql:1470`; produced at `backend/journaling/live_projector.py:58`, `:91`).

**Recommendation (SETTLED).** Make the manual book a first-class, account-scoped virtual book with three properties:

1. **Ingest the account order book.** Poll `kite.orders()` per account and persist orders that no strategy claims. Claiming is by the existing attribution refs (tag/`client_order_ref` → `worker_live_execution_links`). Every observed order is then either claimed (strategy book) or manual (manual book).
2. **Classify the position residual, never guess it.** For each `(instrument, product)`, `broker_quantity − Σ attributed − manual_explained` must be zero; any residual is `unexplained` and raises `action_required` with the exact instrument/product. Carried-forward delivery holdings are a known class and are tagged as such, not treated as new activity.
3. **Manual stays manual.** A manual fill must never be auto-assigned to a strategy. The operator may reclassify (manual → strategy) explicitly, audited, with an adjustment record — never by heuristic.

**Consequence.** The aggregate identity in R1 §3.2(5) becomes computable *and* provable. The missing observation channel (account order-book ingest) is a prerequisite for Q2 and for exit safety on shared accounts.

**Status: SETTLED** — with one safety change that has user-visible consequences (see §2.1 and §4, E-3).

---

### Q2. Opposing virtual positions in the same instrument/product

**Question.** When can A long +100 and B short −20 in the same product coexist safely?

**Research findings.** R1 §3.4 established the proof problem: `broker_net = 80` with `A = +100` is indistinguishable between (a) B's opposing book, (b) an out-of-band manual sale, (c) incomplete attribution. The direct exit validation refuses it (`backend/api/routers/worker_execution.py:220-229`), which is correct *given today's evidence*. Q1's manual book removes cause (b) and (c) — but only if the account order-book ingest exists and no unresolved links remain.

**Recommendation (SETTLED, staged).** Keep opposing same-product exposure deferred in V1, and define the exact gate for enabling it:

```
G1  no unresolved execution links for any book on the instrument/product
G2  account order-book ingested, every observed order claimed or manual      (Q1)
G3  aggregate identity verified at admission and again at exit submission:
        Σ attributed + manual_explained == broker_quantity
G4  account margin headroom covers the sum of book-level margins
G5  product separation preserved (no CNC/MIS mixing in the check)
```

When G1–G5 hold, an exit sized to the strategy's own attributed quantity is arithmetically safe: it changes `broker_quantity` by exactly that quantity and leaves every other book's arithmetic intact. Exits still sized to own book only; no crossing (Q4).

**Consequence.** This is a *sequencing* answer: Q2 cannot be enabled before Q1 + the flatness correction (R1 §4.3). Until then the current refusal is the correct behaviour.

**Status: SETTLED** (gate + sequencing); **the date/priority is an escalation** (§4, E-2).

---

### Q3. Whether any strategy may request exclusive ownership

**Question.** Should a strategy ever be able to lock an instrument against other strategies?

**Research findings.** The platform cannot enforce exclusivity against the human: the owner can always trade in the broker app, producing exactly the manual-book distortion that exclusivity is meant to prevent. A claim lifecycle therefore adds release hazards (settlement gating, operator release, stale claims) while providing a guarantee that is false the moment anyone touches the app. Isolation *is* achievable structurally: `account_scope` is already a first-class scope (`backend/strategies/models.py:199`), and separate broker accounts give real exclusivity with zero new machinery.

**Recommendation (SETTLED).** Do not implement per-strategy exclusive ownership.

- If a strategy genuinely needs isolation, the supported answer is a **separate broker account** (and the platform should say so in the UI).
- Provide a *soft* control instead: an account-scoped, operator-configured **reserved-instrument list** that blocks platform-mediated writers (hosted children and external workers) and is visible in the UI, with an explicit statement that out-of-band trades are not blocked.
- Do not promise exclusivity in product language, because it cannot be guaranteed.

**Status: SETTLED.**

---

### Q4. Eventual account-level order netting

**Question.** Should the platform net strategies' orders against each other before sending?

**Research findings.** Netting means the platform decides which strategy's price/fill a crossed quantity belongs to. That is an **allocation policy**, and it becomes the audit artifact for P&L (§13). Crossing is also not something the broker offers to clients; the platform would be internalizing between its own books. The realistic benefit is a brokerage/slippage saving on the crossed quantity.

**Recommendation (SETTLED).** Never cross books. Do not build an account-level netting allocator. Instead implement a **netting advisor**:

- at admission, when the platform sees opposite intents on the same `(instrument, product)` from two books inside the same evaluation window, it may **sequence** them (for example sell first, then buy) so the account's market impact falls without any crossing;
- the advisor reports the estimated saving and the sequencing decision in the journal;
- each order remains fully attributed to its own strategy.

**Consequence.** All attribution stays fact-based. If cost optimisation ever demands actual netting, it must arrive with a published allocation policy and a fairness audit — a separate revision, not a default.

**Status: SETTLED** as a permanent stance; revisit only with a business case.

---

### Q5. Capital reallocation while positions remain open

**Question.** Can a strategy's allocation change while it holds positions?

**Research findings.** R1 §5.6 fixed capital as a durable, versioned record with reservations. Admission is exposure + in-flight + reserved + plan. Nothing in the current code enforces allocation at all (`backend/api/routers/worker_protection.py:209-222`, reporting-only), so this is greenfield.

**Recommendation (SETTLED except E-4).**

- Capital is a **time-stamped version chain**, never rewritten: `capital_version(v, effective_at, basis, amount, cash_policy, actor, reason)`.
- **Upward** reallocation takes effect at the next admission; nothing retroactive.
- **Downward** reallocation is allowed and does not force liquidation: exposures above the new allocation are **grandfathered**, and `freeze_new_risk` applies automatically until attributed exposure ≤ new allocation (risk vocabulary from R1 §5.7).
- Reservations created under the previous allocation are honoured; unsubmitted plans may be revoked to force re-admission under the new policy (operator choice, recorded).
- P&L and return math use a **capital curve** (time-weighted), not the current number.
- Strategy code can never change its own capital — reallocation is operator-initiated and audited.

**Consequence.** Prevents the two failure modes: silent over-leverage after a cut, and forced liquidation on a paper decision.

**Status: SETTLED**; the grandfathering policy is an escalation (§4, E-4).

---

### Q6. Live proposal auto-execution versus operator approval

**Question.** When does a live proposal execute by itself?

**Research findings.** The preview machinery for approvals already exists (`/preview/order`, `/preview/basket` at `backend/api/routers/worker_execution.py:575`, `:618`; option previews at `backend/options/api/worker_options_router.py:231`, `:267`). Runs already carry `allowed_actions_json` and jobs carry `capabilities_snapshot` (`backend/strategies/models.py:129`, `:216`), so a per-strategy authority flag has a natural home.

**Recommendation (SETTLED except the default, E-1).**

- Per-strategy `live_execution_authority ∈ {notify_only, operator_approval, auto_within_policy}`.
- **Default for new live strategies: `operator_approval`.** Auto is opt-in and only after that strategy passes live certification.
- Approval is **per plan**, not per order: bounded by an expiry window, audited (actor, timestamp, plan hash), revocable, and invalidated if the plan's pinned inputs change.
- Even in `auto_within_policy`, admission, reservations, claims-free attribution rules and protection all apply; auto never bypasses policy.
- `notify_only` exists as the graduation step from paper to live.

**Status: SETTLED**; the default value is an escalation (§4, E-1).

---

### Q7. Conflicting protection actions between strategies

**Question.** What happens when two strategies' protections fire at once, or a stop conflicts with another book?

**Research findings.** Protection today is per run and single-action: any triggered rule yields `exit_strategy` for that run (`backend/api/services/protection.py:365-367`), evaluated every 5 seconds (`backend/app/background.py:50`). Options protection is advisory only. So conflicts have never been modelled — but with shared instruments they can occur in three ways: simultaneous exits on the same instrument, margin contention, and account-level emergency action.

**Recommendation (SETTLED).**

- **Scope rule:** protection acts only on its own strategy's attributed exposure. It never touches another book's quantity.
- **Evidence rule:** if attribution is unproven (unresolved links, unexplained residual), protection may only `freeze_new_risk` and escalate — it must not exit on a possibly-wrong quantity.
- **Serialization:** exits on the same `(instrument, product)` from different strategies are serialized by a per-instrument execution lock, so market impact is not doubled and fills stay separable.
- **Priority:** account emergency (Q12) > strategy protection > proposal policy.
- **Contention:** margin contention is resolved at admission by reservations, first-come with operator override; never by an implicit netting assumption.

**Status: SETTLED.**

---

### Q8. Shared option contracts across structures

**Question.** What if two structures hold the same contract?

**Research findings.** Options positions net at the broker per contract/product exactly like equities, and attribution works the same way (fills → links → strategy). Two structural differences matter: (a) broker margin is netted *across* books, so per-structure margin can over- or under-state reality; (b) option-specific settlement (Q9).

**Recommendation (SETTLED).**

- Sharing is allowed with the same attribution rules as equity.
- **Structure-level margin and max loss are computed from the structure's own legs** (defined-risk from the frozen plan). The broker's netted account margin is a *separate binding constraint*; the platform reports the netting benefit but never distributes it to structures as extra capacity.
- The intra-structure hedge rule ("a hedge fill releases only the matching short quantity") does not extend across structures: structure 1's long is not structure 2's hedge for risk or settlement purposes.
- Product separation applies; **options are NRML-only in V1** (MIS options add intraday expiry hazards for no design benefit).
- Expiry: each structure must satisfy the Q9 policy independently, even if another structure holds the same contract.

**Status: SETTLED.**

---

### Q9. Assignment/exercise and expiry settlement

**Question.** What happens to open option positions at expiry?

**Research findings.** There is **no** expiry, exercise or settlement logic anywhere in the backend (search over `expiry_settlement|physical_settlement|exercise|settlement_price` returns only unrelated docstrings). Option runs carry `expiry_key` metadata only. Index options are cash-settled; single-stock derivatives are physically settled, so an ITM long can create a delivery obligation and an ITM short can be assigned — both **without any order or fill**, which is exactly the corporate-action shape (external adjustment, not a trade).

**Recommendation (SETTLED except the default policy, E-5).**

- **Default policy `flatten_shorts_before_cutoff`:** every structure with short legs is flattened before the exchange cutoff by a risk-reducing, audited platform action; never rely on broker auto square-off.
- **Expiry produces adjustment records, never synthetic trades** — reuse the corporate-action adjustment machinery (immutable record, plan/protection invalidation, freeze new risk, operator evidence).
  - Index (cash-settled): a cash adjustment attributed to the owning strategy's book, from the frozen legs, at the settlement price.
  - Stock (physically settled): an ITM long creates the underlying position via an adjustment record; assignment on a short creates a short/delivery obligation via an adjustment record.
- **Opt-in `allow_physical_settlement`** per strategy is required to carry ITM longs into settlement; without it the platform flattens.
- Block *new* positions in a contract inside a configurable window before expiry unless the strategy explicitly allows settlement.
- Do not hardcode tax rates: STT on exercise differs from sell-side STT and changes with policy; charges arrive through `charges_status` (`estimated` → `broker_quoted` → `reconciled`) per §13.

**Status: SETTLED**; the default policy and the opt-in are an escalation (§4, E-5).

---

### Q10. Merger/demerger attribution

**Question.** How do successor instruments receive a strategy's position?

**Research findings.** Same class as Q9: identity changes (unlike a split, where `instrument_id` survives). Ratios vary, cash components are common, and no feed in this repository carries them (corporate actions appear only in fundamentals/mutual funds).

**Recommendation (SETTLED).**

- Immutable adjustment record: source instrument, successor instrument(s), per-successor ratio, cash component, record/effective dates, source document reference, verification status.
- Allocation: each strategy's **pre-event attributed quantity** is allocated across successors pro-rata to the ratio; fractional residuals become cash adjustments.
- Plans and protection on affected instruments are invalidated (R1 §5.4: only relevant changes invalidate); new risk is frozen until a fresh evaluation.
- Operator-verified in V1; automation only for narrowly supported action types with a credible source.

**Status: SETTLED.**

---

### Q11. External-worker compatibility migration

**Question.** How do external workers keep working when admission and attribution tighten?

**Research findings.** External workers today hold the full raw surface (orders, baskets, brackets, options). Runs already carry `allowed_actions_json` and jobs carry `capabilities_snapshot` (`backend/strategies/models.py:129`, `:216`); the SDK pins an endpoint manifest (`sdk/python/kite_algo_worker/endpoint_manifest.py:53-95`). Admission endpoints already exist as previews (`backend/api/routers/worker_execution.py:575`, `:618`).

**Recommendation (SETTLED).**

1. **Capability set version.** The run fetch returns an explicit capability version; every mutation asserts it. Servers are authoritative; SDK removal alone is never the control.
2. **Authoritative preview.** Promote `POST /preview/order|basket` from informational to authoritative: it returns the same admission decision (including reservations that would be taken) and a short-lived **reservation token**; placing the order with that token cannot then be refused for admission reasons. This removes mid-run surprises for external workers.
3. **Same rules for every platform-mediated writer on a shared live account** — hosted or external. Refusals are named codes.
4. **Migration matrix** published: for each SDK method, its proposal-based replacement (hosted) or its raw status (external), plus deprecation policy (warn one release, keep N-1 compatibility, never silently reinterpret an old call as a new proposal).
5. Accounts without hosted strategies may keep a permissive profile, but admission still applies — it is what makes the numbers true.

**Status: SETTLED.**

---

### Q12. Account-level emergency controls

**Question.** What is the kill switch, who can pull it, and what does it touch?

**Research findings.** Nothing account-level exists (search: no kill switch, no account flatten; `EMERGENCY` in the codebase is only the option strategy's *strategy-level* rule role, `backend/options/strategy/models.py:29`). Existing risk-reducing precedents are per run (`exit_on_worker_stale` at `backend/api/services/protection.py:511-521`; stale/exiting recovery at `backend/api/services/runtime_recovery.py:167-331`).

**Recommendation (SETTLED except authority, E-6).**

`ACCOUNT_EMERGENCY` is an audited incident with an ordered, idempotent, resumable action set:

1. `freeze_new_risk` — all writers, both adapters;
2. `cancel_pending_all` — platform-tracked orders only;
3. `exit_all_strategy_exposure` — per-strategy attributed exits, serialized per instrument;
4. `revoke_execution_authority` + `disconnect_workers` — supervisor-level stop.

Rules: the emergency artifact must state its own scope honestly — it **cannot** control the manual book or out-of-band broker actions, and its report must enumerate residual manual exposure. It does not stop reconciliation; settlement work continues after the emergency. Operator-triggered by default; auto-triggering only from platform guardrails, and never on unproven attribution.

**Status: SETTLED**; authority and auto-trigger policy is an escalation (§4, E-6).

---

### Q13. Fee and tax attribution

**Question.** Which strategy pays which charge?

**Research findings.** Charges are already per fill and componentised: `journal_execution_facts` carries `gross_cash_flow`, `brokerage`, `stt`, `stamp_duty`, `sebi_charge`, GST and a `charges_status ∈ {estimated, broker_quoted, reconciled, unavailable}` (`backend/schema.sql:1761-1772`, `:1830`). Fill-level attribution therefore already has a home; what is missing is a policy for charges that are not fill-level, and lot-level cost basis in live (paper has lots: `backend/schema.sql:880`).

**Recommendation (SETTLED).**

- **Fill-level charges** (brokerage, STT, stamp duty, exchange txn, SEBI, GST on those): attach to the strategy that owns the fill via links. Deterministic already.
- **Order-level minimums** (per-order brokerage floors, call-and-trade): the order's strategy.
- **Account-level charges** (AMC, DP, pledge, auto-square-off penalties, auction settlement): an explicit, versioned **allocation policy**, default pro-rata by attributed turnover, with the manual book absorbing its share first. Operator-visible, because it affects comparability.
- **Never invent charges**: values stay `estimated` until the broker's quote/contract note reconciles them.
- **Tax reporting is deferred, not attribution**: capital-gains computation needs lot-level cost basis with holding periods. Paper has it; **live lot-level basis does not exist** and is a prerequisite for any tax report (§2.3).

**Status: SETTLED.**

---

### Q14. Late broker corrections after strategy settlement

**Question.** What if the broker reports a fill or charge after a strategy has settled?

**Research findings.** Settlement today is blocked for live entirely (R1 §2.5, §4), so this is prospective. But the shape is known: brokers post late fills, price corrections, square-off penalties and contract-note adjustments.

**Recommendation (SETTLED).**

- **Settlement is terminal for execution authority only.** A correction never derives new orders and never silently revives a strategy.
- A late correction becomes an append-only `post_settlement_adjustment` against the strategy's book, updating attributed quantity and P&L, followed by a re-verification of the account identity.
- If a correction creates exposure, the strategy enters `action_required` with a correction work item; acting requires a **fresh evaluation** (R1 §5.3), never the old authority.
- Corrections that cannot be linked go to the **manual book**; they are never guessed onto a strategy.
- **Finalization window:** exposure settlement should require the broker's end-of-day evidence (positions and trades complete), not merely the platform's own sync completion. Most corrections then land *before* settlement instead of after it, which is the real mitigation.

**Status: SETTLED.**

---

## 2. Cross-cutting findings discovered while answering

### 2.1 A silent misattribution path exists today (safety)

`resolve_external_fill_run` attaches an untagged broker fill to a strategy's journal whenever **exactly one** open live run shows a reducing book; otherwise the fill becomes `broker_import` (`backend/journaling/live_projector.py:41-58`). With two strategies on one instrument the heuristic is already fragile; with a manual exit on an instrument held by exactly one strategy it books a manual trade to that strategy's journal and P&L. Q1's manual book makes the correct rule possible: **claim by tag or by explicit operator reclassification; otherwise manual.** Recommended correction is part of Q1.

### 2.2 The missing observation channel is the account order book

Several answers (Q1, Q2, Q14) depend on knowing every order the account placed, not just the platform's. `kite.orders()` is available and already used (`backend/broker_api/orders/service.py:440-444`), but nothing persists unclaimed orders. This single ingest is the highest-leverage addition in R2's dependency order (§3).

### 2.3 Live lot-level cost basis does not exist

Paper keeps per-lot attribution (`backend/schema.sql:880`; writer `backend/paper_runtime/repository.py:888`). Live keeps links and fills but no lot ledger, so realised P&L is derived and capital-gains reporting is impossible. This blocks Q13's tax half and any "long-term vs short-term" product feature; it is recorded as a prerequisite rather than designed here.

---

## 3. Updated dependency order (delta on R1 §8)

1. Correct and certify fill attribution and per-strategy exit/settlement (R1 §4.3).
2. **Account order-book ingest + manual book (Q1, §2.2).**
3. Aggregate account reconciliation across books (Q1's identity).
4. **Remove the unique-candidate heuristic (§2.1) once the manual book exists.**
5. Durable strategy identity across attempts.
6. Durable capital/risk reservations (Q5 policy hooks here).
7. Proposal persistence and immutable plan model.
8. `target_position` and `intent_bundle`.
9. `target_weights` and portfolio compiler.
10. Trading-calendar scheduler and checkpoints.
11. Deterministic paper partial fills.
12. Futures resolver and roll orchestration.
13. `target_option_structure` bound to the option-run engine, with the Q9 expiry policy.
14. Corporate-action detection (Q9/Q10 share the adjustment machinery).
15. **Capability versioning + authoritative preview for external workers (Q11).**
16. Paper and live certification by scenario.

Opposing same-product exposure (Q2) is unimplementable before steps 1–4 and is not on this list.

---

## 4. Escalations — decisions only you can make

Each entry: the exact question, options, R2's recommendation, the cost of choosing differently, and whether it blocks paper or live.

**E-1. Default live execution authority.**
*Question:* for a newly created live strategy, does a proposal execute automatically or wait for operator approval?
*Options:* (a) `operator_approval` default, auto opt-in after certification; (b) auto default with policy bounds; (c) approval required always until manually relaxed.
*Recommendation:* (a). *If you choose (b):* a strategy bug becomes a market event before a human sees it, and "auto within policy" must then be trusted on day one. *If (c):* unattended strategies cannot run unattended, which contradicts the product. *Blocks:* live only.

**E-2. When opposing same-product exposure is enabled (if ever).**
*Question:* is the Q2 gate a V2 target or explicitly out of scope for the product?
*Options:* (a) V2 after the gate (G1–G5) is met and certified; (b) never, and pairs that want to be opposite must use separate accounts; (c) enable earlier behind an operator flag without certification.
*Recommendation:* (a). *If (b):* you permanently forbid a legitimate pattern (a long book and a short book on one name) — acceptable but should be said in product language. *If (c):* the platform would take naked-short risk on unproven evidence. *Blocks:* live only (paper can simulate).

**E-3. Removing the unique-candidate fill heuristic (§2.1).**
*Question:* do we accept that manual exits stop being auto-attributed to a strategy's journal?
*Options:* (a) remove it, manual book owns unclaimed fills; (b) keep it but require the strategy's book to be the only *attributed* book and mark the fill "provisional attribution"; (c) keep as-is.
*Recommendation:* (a). *If (b):* keeps the convenience but reintroduces misattribution under ambiguity. *If (c):* P&L is knowingly wrong in a common manual-trading case. *Blocks:* paper (journaling correctness) and live.

**E-4. Downward capital reallocation.**
*Question:* when allocation drops below open exposure, grandfather or force-deallocate?
*Options:* (a) grandfather + automatic `freeze_new_risk` until exposure fits; (b) forced reduction plan (deterministic top-down trim); (c) refuse the reallocation until flat.
*Recommendation:* (a). *If (b):* a risk decision (which positions to cut) becomes a capital-administration side effect. *If (c):* the operator cannot reduce a strategy's mandate while it holds anything. *Blocks:* live only.

**E-5. Expiry settlement default.**
*Question:* for stock options at expiry, flatten by default or allow physical settlement?
*Options:* (a) `flatten_shorts_before_cutoff` default, `allow_physical_settlement` opt-in; (b) always flatten; (c) always allow settlement.
*Recommendation:* (a). *If (b):* deliberate delivery-based strategies become impossible. *If (c):* delivery obligations and assignment appear without an order and surprise the operator. *Blocks:* live only (paper can simulate both).

**E-6. Emergency-control authority and auto-triggering.**
*Question:* who may fire `ACCOUNT_EMERGENCY`, and may a guardrail fire it automatically?
*Options:* (a) operator-only, guardrails only `freeze_new_risk`; (b) guardrails may also force-exit below a hard loss budget; (c) guardrails may force-exit on any configured policy breach.
*Recommendation:* (a). *If (b):* add it only with a conservative, audited, single-use trigger. *If (c):* automatic full liquidation becomes reachable from a mis-tuned limit. *Blocks:* live only.

**E-7. Netting stance as a permanent product position.**
*Question:* is "never cross books, sequence instead" (Q4) accepted as a permanent product stance?
*Options:* (a) permanent; (b) revisit if cost data justifies it; (c) build netting behind a flag.
*Recommendation:* (a). *If (b):* fine, and the advisor's logged data is the evidence base. *If (c):* it forces an allocation policy and a fairness audit before there is a business case. *Blocks:* neither — it constrains future scope, not the current build.

---

## 5. Corrections and refinements to R1

| # | R1 said | R2 refines |
|---|---|---|
| 1 | Manual/unattributed: "no manual book of record" (E-scenario table) | Manual book specified as first-class, order-ingested, residual-classified (Q1) |
| 2 | Opposing same-product exposure "deferred … until evidence proves safe" (§3.4) | Explicit gate G1–G5 and the sequencing that makes it reachable (Q2) |
| 3 | Protection "may only reduce risk" (§5.7) | Adds the evidence rule: unproven attribution permits only `freeze_new_risk` (Q7) |
| 4 | Corporate-action adjustment machinery (§5.11) | Reused for expiry settlement and merger/demerger (Q9, Q10) |
| 5 | Settlement levels (§5.5) | Adds the finalization window: broker end-of-day evidence, not platform sync, is the settlement gate (Q14) |
| 6 | Not addressed | Live lot-level cost basis is a prerequisite for tax reporting (§2.3) |
| 7 | Not addressed | The unique-candidate fill heuristic is a misattribution path and must be removed (§2.1) |

---

## 6. Verification note

- Citations refer to the repository state at the commit adding this document.
- "Search returns nothing" claims (expiry/exercise settlement, account emergency controls, lot-level live basis) were produced by case-insensitive searches across `backend/` during this revision.
- R1 remains the decision record for everything it locked; R2 changes only the items listed in §5, plus the answers and escalations above.
