# Hosted strategies — Project 9 parity matrix (futures contracts, full-fill-gated rolls, margin precheck)

Status 2026-09-17, branch `development`. Plan: `docs/superpowers/plans/2026-09-17-futures-rolls.md` (D-1…D-6 binding).

## Delivered
- Migration `20260917_000033` (additive): `strategy_rolls` (BOTH instrument identities retained; required/proven quantities; state machine acquiring→proving_filled→releasing_old→completed|action_required; peak-margin evidence; plan FK + strategies composite FK) + `strategy_roll_events` (append-only trigger).
- `compiler/futures.py`: `target_futures` resolved against the PINNED catalog generation (instrument_type, expiry, lot_size, tick_size, underlying now selected by the pinned read); lot semantics; `CONTRACT_UNRESOLVED`/`EXPIRY_UNAVAILABLE`/`FREEZE_LIMIT_EXCEEDED`.
- **The roll invariant (R3 §13 locked decision 6)**: acquire → prove FULL replacement filled (proof = attributed book on the new contract at its pinned identity, from the G1 projection — never an order-status label) → only then release the close → old book PROVEN flat (Phase 5 axes) → completed. Partial/stalled ⇒ `action_required`, old attribution intact, NEVER auto-reverse. `release_close` raises `ReleaseRefused` naming the invariant. **Basket `all_or_none` irrelevance pinned by a negative test** (and AST-verified at scope audit). One open roll per (strategy, old contract) — including `action_required` — enforced under a PostgreSQL advisory lock with under-lock re-check.
- Peak-margin admission precheck: peak = concurrent old+new exposure; compared against a SUPPLIED margin capacity (paper margin engine evidence); no capacity supplied ⇒ axis records the peak without enforcing (honest, consistent with live quotes being future wiring); `MARGIN_INSUFFICIENT` with evidence; preview ≠ reservation.
- Expiry-cutoff escalation: unrolled expiring contract inside `FUTURES_EXPIRY_WARNING_DAYS` (default 5) ⇒ `action_required` + single owner notification.
- Owner reads: `GET .../rolls`, `GET .../rolls/{roll_id}` (owner + account authorization, `extra="forbid"`).

## Test evidence (independently re-run by the orchestrator)
- Unit: 13 compiler + 20 rolls + 13 margin/expiry + 2 API; **617 passed / 1 skipped** strategies+owner-API sweep.
- **PG:** `FUTURES_PG_URL=…` → **13 passed**: head + upgrade-from-prior-head; roll-with-partial-new-fill walkthrough (stalls `action_required`, attribution intact, no auto-reverse); close-step unreachable before full fill; basket-flag negative test; peak-margin refusal; expiry escalation; concurrent duplicate roll refused; insert-only trigger (it rejected the agent's own cleanup DELETE — the test now uses distinct contracts per case). Skip proven without URL.
- `tests/api` baseline identical; single head `…000033`; `git diff --check` clean; no options code touched; no proportional release (AST-verified); live gate intact.

## Deviations (accepted)
- **Freeze axis unimplementable as specified** (the one plan-vs-source contradiction): no freeze-quantity column exists in the records schema, published view, or mapping table. The limit comes from the intent's declaration; undeclared records carry `freeze_source: unavailable` so the unchecked axis is visible. Catalog freeze metadata is a future catalog work item.
- Peak compared against supplied margin capacity, not notional allocation (conflating the two would mis-refuse or mis-allow).
- Roll opens take an advisory lock (a plain read-then-write uniqueness check loses under concurrency).
- `action_required` counts as an open roll (a stalled roll is unresolved).

## Limitations / NOT PROVEN
Paper-only (`PAPER_ONLY_EXECUTION`); live margin quotes are future wiring; proportional release deliberately absent (requires its own decision); catalog freeze metadata absent (declared-source fallback). Real broker behaviour/deployment NOT PROVEN.

## Gate summary
Roadmap Project 9 acceptance evidence Closed (partial-fill roll walkthrough, margin-peak precheck, expiry-cutoff escalation). Phase gate: **PASSED**.

---

## Evidence class note 2026-09-21

The evidence in this report is **component/unit** (and, where stated, route or
PostgreSQL) evidence. It is NOT a production-route or paper end-to-end result, and
it does NOT certify live behaviour. See
`documents/hosted-strategies-integration-closure.md` for the corrected phase and
migration arithmetic (eleven phases 0..10; original migrations 000025..000034,
closure migrations 000035..000038, head `20260921_000038`) and for the named
remaining blockers (no hosted live execution mode, live fill-ingestion not bound,
no live market certification).
