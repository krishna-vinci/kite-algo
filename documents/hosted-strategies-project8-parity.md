# Hosted strategies — Project 8 parity matrix (MIS intraday lane — policy, square-off, stale exit)

Status 2026-09-17, branch `development`. Plan: `docs/superpowers/plans/2026-09-17-mis-intraday-policy.md` (D-1…D-6 binding).

## Delivered
- Migration `20260917_000032` (additive): `strategy_squareoff_evidence` (insert-only trigger, outcome CHECK squared_off|action_required|missed_by_broker|stale_worker_exit).
- **MIS is intraday by policy**: horizon read from the intent's declaration (`hold_days`/aliases, default intraday — never inferred); multi-day MIS refused `MIS_OVERNIGHT_REFUSED` at validation (ends the evaluation, Phase 3 semantics) naming CNC/NRML for longs and futures/options for shorts; the rule lives in `single_instrument` so `intent_bundle` inherits it; same-session plans accepted.
- **Square-off platform-owned, semantics untouched**: per-product schedules (NSE/BSE 15:20, NFO 15:25, CDS 16:45, MCX 23:20, override honoured) resolved via the PRODUCTION schedule resolver (imported, not copied); exits through the durable claim path, clamped to attributed quantity; every outcome lands in evidence. **`protection.py`/`protection_runtime.py`/`runtime_recovery.py` have ZERO diff** — stronger than the plan permitted.
- Failed square-off → `action_required`, keeps reconciling, never settlement; broker auto-square-off recorded as `missed_by_broker` (fallback, never control).
- Stale-worker exit attached at the policy level (not the protection runtime — no second liquidation authority), through the claim path, never oversized.
- **Partial-fill tick certification (Phase 7's carried prerequisite) CLOSED**: deterministic synthetic-price tick driver; the partially-filled rebalance completes across ticks end-to-end (renewal on verified progress, convergence, barrier honesty, settlement only at true quiescence). The tick source is synthetic and stated in the test — the invariants do not depend on a market-data runtime.
- Owner read: `GET /api/strategies/{id}/squareoffs?environment=` (owner + account authorization, `extra="forbid"`).

## Test evidence (independently re-run by the orchestrator)
- Unit: 10 mis_policy + 14 harness + 10 stale-exit + 5 tick tests; **569 passed / 1 skipped** strategies+owner-API sweep; evaluation-spent semantics preserved (31 proposals tests).
- **PG:** `MIS_PG_URL=…` → **13 passed**: head + upgrade-from-prior-head; **walkthrough 2** (MIS B beside CNC A on one equity — separate product books, B cannot sell A's shares, A untouched); square-off failure → action_required + reconciliation continues; multi-day refusal end-to-end; per-product schedules incl. override; tick certification; insert-only trigger. Skip proven without URL.
- `tests/api` baseline identical; single head `…000032`; `git diff --check` clean.

## Self-caught defects (fixed pre-landing)
Inverted exit-size direction check (refused a legitimate sell); evidence view built after session close; tick driver totalling fills from open remainders ("a fill that un-happened" — exactly what the monotonicity assertion catches).

## Limitations / NOT PROVEN
Real broker square-off behaviour and the full paper service tick loop (synthetic tick source in tests); live MIS enablement remains closed (`PAPER_ONLY_EXECUTION`) pending separate authorization; overnight MIS and SLB are permanent non-goals.

## Gate summary
Roadmap Project 8 acceptance evidence Closed (walkthrough-2 + square-off-failure). Phase gate: **PASSED**.
