# Hosted strategies — Project 2 parity matrix (strategy closure, account truth, reconciliation — G2+G3+G4)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 2](hosted-strategies-implementation-roadmap.md):
closure from the attributed book, account-wide order/fill truth, the manual book,
aggregate reconciliation as a checked invariant, divergence classification and
append-only attribution adjustments. Plan:
`docs/superpowers/plans/2026-09-17-strategy-closure-manual-book-reconciliation.md`
(R3 §10/§17/§22 authored; design decisions D-1…D-6 binding).

> **Read this first.** **Closed** = implemented + covered by an executed test in
> this repo (most database/classification claims proven on real disposable
> PostgreSQL). **NOT PROVEN** = requires separate authorization/infrastructure.

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Migration `20260917_000026` (single head, after `000025`; purely additive) | Closed — `broker_trade_facts` (insert-only, dedupe `(account_id, trade_id)`), `account_ingest_state`, `strategy_reconciliation_state` (classification + refresh bookkeeping), `strategy_attribution_adjustments`/`_lines` (insert-only triggers; line composite FK `(strategy_id, owner_id, account_id) → strategies (id, owner_id, account_scope)`); mirrored in `backend/schema.sql` |
| G2 — closure correctness (account flatness never substitutes for strategy flatness) | Closed — bound runs read the strategy-position projection for flatness (`runtime_recovery.py`), exposure (`worker_shared.py`) and exit sizing; unbound (legacy) runs keep run-scoped behavior; the `# Phase 2` placeholder is replaced; proof: strategy A flat while B holds the same broker line |
| G3 — account-wide fill truth + manual book | Closed — ingest via the broker account-wide trade book (`OrdersService.trades`, `service.py:502`): tracked AND untracked fills persist insert-only into `broker_trade_facts`; manual residual = signed unlinked facts (− adjustment deltas); supervised background cycle with per-account failure isolation |
| Heuristic removal (R3 §17) | Closed — `resolve_external_fill_run` is unconditionally broker-import; the unique-reducing-candidate rule is deleted and its regression pinned (an untagged reducing fill NEVER attaches to the only matching open run) |
| G4 — reconciliation, classification, freeze | Closed — per `(account, token, exchange, symbol, product)`: `aligned` / `pending_ingest` / `unexplained`; **both non-aligned classes freeze new exposure immediately**; bounded refresh (default 3 attempts, env-tunable) re-reads truth in the same check that refreshed; persistent mismatch after the bound (and a clean ingest state) → `unexplained`; freeze decision is a pure function (`reconciliation_refusal`) admitting only risk-reducing orders, refusal named `RECONCILIATION_FREEZE` with the coordinate and class |
| Negative manual residual is explicit (walkthrough 7 case 2) | Closed — the existing one-sided exit guard still refuses over-exit and its refusal detail names the reconciliation state |
| Owner escalation | Closed — once per unexplained coordinate through the durable notification outbox; recipients = distinct `strategies.owner_id` on the account (no invented owner model); unresolvable recipient → retry later (`owner_notified_at` unset); notification failure never blocks classification or the freeze; idempotency key prevents re-sends |
| Append-only reclassification adjustments | Closed — `POST /api/strategies/{id}/adjustments` (owner-only, G1 grant-issuance checks reused, `ConfigDict(extra="forbid")`, one transaction); lines fold as TradeFacts with stable source identity `adjustment:<id>:<line_no>` (live book); claimed −10 fill moves A 100→90 and manual −10→0; correcting line reverses; rebuild folds each adjustment exactly once by construction |
| Control plane | Closed — `_unattributed_bucket` reads persisted classification/manual state (display parity on aligned data) |

Commits (unsigned, on `development`, not pushed): `31dffc8` (schema), `15c9964` (G2 closure), `813fefe` (G3 ingest + heuristic removal), `ae8bf17` (G4 reconciliation/freeze/escalation), `f19e0c1` (adjustments), `dd26fd9` (PostgreSQL integration).

## 1. Test evidence (executed 2026-09-17, this machine; counts independently re-run by the orchestrator)

| Suite | Result |
| --- | --- |
| `tests/strategies/test_account_truth.py` | 21 passed |
| Binding + worker + hosted API suites | 203 passed |
| `pytest tests/strategies -q` | 209 passed, 1 skipped |
| **PostgreSQL:** `TRUTH_PG_URL=… pytest tests/integration/test_strategy_account_truth_postgres.py -q` | **13 passed** (invariant walkthrough 7 end-to-end; pending_ingest→aligned and →unexplained transitions; freeze symmetry + coordinate scoping; insert-only triggers; adjustment-line composite FK; rebuild-vs-adjustment concurrency; closure independence; heuristic-removal regression; concurrent ingest dedupe). Without a URL: **1 skipped — never fake-passed** |
| `tests/journaling` | 154 passed, 2 failed — both pre-existing `test_journal_filters` failures, proven byte-identical with the live_projector change stashed |
| `pytest tests/api tests/strategies` vs baseline | identical to the documented 20-failure env/config baseline — no regressions |

## 2. Decisions and deviations (reviewed and accepted by the orchestrator)

- **Single-writer ingest:** Task 3 did not modify `order_runtime.py` — account-wide ingest reads `OrdersService.trades()` (one call covering tracked + untracked), keeping dedupe in one writer. The plan's named file was superseded by a cleaner path to the same invariant.
- **Freeze decision is a pure function** (`reconciliation_refusal` + `coordinate_of`); `_assert_not_frozen` reads persisted state — exhaustively testable without a broker.
- **Post-refresh re-read:** after a successful bounded refresh, classification re-reads the numbers in the same check, so a freeze lifts immediately when truth arrives (both branches pinned: no refresh available → pending_ingest; refresh available → aligned in-check).
- **Escalation recipients:** `strategies.owner_id` on the account names the person; `authorized_account_scopes()` yields scopes, not identities, so it cannot — no second notification subsystem invented; `run_id=f"reconciliation:{account_id}"` keys the run-notification outbox for a non-run condition.
- **`create_reclassification` delegates to `SqlAttributionStore`** — adjustment state lives beside the fold that consumes it.
- **ORM for new tables / `public.`-qualified SQL for pre-existing** — the Phase 1 dual-engine pattern (three methods initially used `public.` for new tables and silently returned nothing on SQLite; fixed).
- **`fact_id` is a real UUID** (the PG column is UUID; a readable string was refused).
- Schema realities carried from Phase 1 and re-confirmed: `mapping_ambiguous` unreachable via mapping uniqueness (defensive branch stays); one strategy = one account for life (tests use same-account/two-environments deliberately).

## 3. Known limitations and deferrals

- Corporate actions are NOT detected (Project 7): a sudden large residual classifies `unexplained` and freezes — the desired fail-closed behavior until detection lands.
- The reconciliation identity is live-book only; paper books remain platform-internal (by design).
- Freeze enforcement covers worker-submitted live placement paths; protection square-off exits are inherently reducing and unaffected.
- The 2 pre-existing `tests/journaling::test_journal_filters` failures are unrelated (pre-date Phase 2; proven by stash comparison) and remain on the known-failure list.
- Escalation message addresses strategy owners, not arbitrary account viewers; UI surfacing of reconciliation state is Phase 3+ work.

## 4. NOT PROVEN (requires separate authorization)

- Real Kite account-wide trade behaviour (real `trades()` payloads at scale, partial history windows) — all broker interaction faked in tests.
- Deployment/migration against any real database; real owner notifications (outbox reached only via injected notifiers in tests).

## 5. Gate summary

All roadmap Project 2 requirements and invariants (1–5) are Closed with executed
tests, including real-PostgreSQL proof of triggers, transitions, freeze symmetry,
the §17 walkthrough and append-only adjustment semantics. **Paper/live
certification:** N/A this phase (no new execution lane; live behaviour NOT PROVEN
per §4). Phase gate: **PASSED**.

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
