# Hosted strategies — Project 7 parity matrix (CNC portfolios, scheduling, paper partial fills, corporate actions — G11+G12+G13)

Status 2026-09-17, branch `development`. Plan: `docs/superpowers/plans/2026-09-17-cnc-portfolios-scheduling.md` (D-1…D-6 binding).

## Delivered
- Migration `20260917_000031`: `strategy_schedule_occurrences` (unique per schedule+occurrence), `paper_order_fill_progress`, `strategy_corporate_action_events` + append-only event log; **two CHECK widenings** (`ck_hosted_strategy_schedules_kind` += monthly/calendar; `ck_spee_event` += partially_filled) — supersets via the universes CHECK precedent, downgrade restores originals.
- `compiler/weights.py`: deltas vs the strategy's attributed book; **sells before buys**; gross cash reservation for all buys upfront; full-snapshot sell-to-zero for omitted members (real full-snapshot plans carry every in-scope member, omitted as explicit zeros); fit refusals by name (`INSUFFICIENT_PORTFOLIO_CASH`, gross/instrument notional, max open); `REFERENCE_PRICE_UNAVAILABLE`/`ADMISSION_POLICY_MISSING` passthroughs.
- `scheduling.py`: monthly/calendar occurrence materialization; fencing via the occurrence unique constraint; misfire grace (`SCHEDULE_MISFIRE_GRACE_SECONDS` default 3600, else skip+journal); overlap skip while prior occurrence unresolved; per-occurrence new evaluation_id + proposal, SAME strategy book; supervised background loop with per-schedule isolation.
- Paper partial fills (opt-in: `PAPER_PARTIAL_FILL_RATIO` **defaults 1.0 = instant-full** — knowingly departed from the plan's 0.5 so Phase 6 certified semantics are untouched; the ratio enables the behaviour): tranche fills (ceil, min 1 — geometric convergence, tested to completion); executor outcome `partially_filled`; no `work_resolved` while a remainder is open (barrier honesty); reservation `renew`/`require_action` carry evidence; `_open_remainders` fails closed (unreadable progress never releases capacity).
- Corporate actions: detection wired in `reconcile_account` (not the live-path order_runtime — paper-only phase); split-like changes are NEVER absorbed; `strategy_corporate_action_events` + append-only log; the coordinate freezes as `unexplained` with named reason `SUSPECTED_CORPORATE_ACTION`; single owner escalation via injected notifiers; resolution ONLY via an owner-verified append-only adjustment; monthly book visibly frozen, never rebased.

## Test evidence (independently re-run by the orchestrator)
- Unit: 17 weights + 16 scheduling + 18 partial-fills + 14 corporate-actions; **518 passed / 1 skipped** strategies+owner-API sweep.
- **PG:** `CNC_PG_URL=…` → **21 passed**: head + upgrade-from-prior-head; **month-over-month walkthrough** (September's 100 RELIANCE visible as October's current holdings; October trades only the delta); sells-before-buys; cash-reservation fit refusal; corporate-action divergence + freeze + escalation; scheduler misfire/overlap under concurrency; skip proven without URL.
- `tests/api` baseline identical; single head `…000031`; `git diff --check` clean; one pre-existing `tests/paper_runtime` failure proven unrelated (stash comparison).

## Certification status (honest)
Month-over-month walkthrough and corporate-action divergence are certified **end-to-end on PostgreSQL**. Partial fills are certified at unit + PG level for tranches, executor outcomes, reservation consequences and convergence — but **not yet as a full execute-a-rebalance-across-ticks run** (needs the paper tick scheduler wiring). **Carried to Phase 8** as a certification prerequisite. Live CNC enablement remains closed (`PAPER_ONLY_EXECUTION`) — enabling it requires separate authorization plus that final partial-fill tick certification.

## Gate summary
All roadmap Project 7 acceptance evidence Closed to the depth available in this phase, with the one limitation above explicitly carried. Phase gate: **PASSED**.
