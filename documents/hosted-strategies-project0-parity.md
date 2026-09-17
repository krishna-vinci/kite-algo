# Hosted strategies — Project 0 parity matrix (live order-mutation ownership hardening, G14)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 0](hosted-strategies-implementation-roadmap.md):
worker `cancel_worker_order` / `modify_worker_order` must refuse any order not proven
to belong to the requesting run's authorized account, before any broker mutation call.
Detailed plan: `docs/superpowers/plans/2026-09-17-worker-order-ownership-hardening.md`.

> **Read this first.** Status labels: **Closed** (implemented and covered by an executed
> test in this repo), **Verified locally** (executed here, on this machine, command
> shown), **NOT PROVEN** (a live action requiring separate authorization or
> infrastructure). "Closed" means the *mechanism* is closed by automated tests, not
> that live broker behaviour has been observed. Live-market certification is out of
> scope for this campaign phase (see §7).

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Conflict-aware ownership lookup `get_live_order_ownership` | Closed — `backend/api/repositories/algo_worker_repo.py:324` (+ `_get_live_order_ownership_sync` `:1460`); link authoritative, intent fallback, DISTINCT-owner sets, conflict on multi-owner or link-vs-intent disagreement |
| Ownership guard on live cancel | Closed — `backend/api/routers/worker_execution.py:272` (`_require_worker_order_ownership`), wired at `:610` |
| Ownership guard on live modify | Closed — same guard wired at `:634` |
| Read paths | Deliberately unchanged — `get_worker_order` / `get_worker_order_history` keep their snapshot-based checks (`worker_execution.py` `:504`/`:524` region); read vs mutation semantics stay distinct |
| Broker client (`orders/service.py`) | Untouched — cancel/modify remain raw broker calls; every refusal happens before they are reachable |
| Schema/migrations | None. Alembic head remains `20260915_000024` (asserted by the PostgreSQL suite) |

Commits (unsigned, not pushed; `commit.gpgsign=true` is set but the key is unavailable,
so `--no-gpg-sign` was used per campaign authorization):

| Commit | Subject |
| --- | --- |
| `e7230588e5dd81788d910265ea1e85e871a0393f` | feat(algo-worker): conflict-aware live order ownership lookup |
| `eb99d42dfc44a7fdf892b3586e9b3f265411768a` | fix(algo-worker): require proven or authoritatively-parented ownership before live cancel |
| `2cbda82a4d92b174bce3ae497bd7ae9e24870361` | fix(algo-worker): require proven ownership before live modify |
| `3efb25f7b452cad58452ca794fcb6f4689fee746` | test(algo-worker): pin fencing precedence and non-disclosure for order mutations |
| `ad25837be5c0c34c9c7d99f5f985aa4d1f257d4e` | test(algo-worker): postgres integration for ownership precedence, conflicts, concurrency |

## 1. Invariant matrix (roadmap invariants 1–9)

| # | Invariant | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Mutation never called when ownership unproven; read-only broker query allowed on refusal path | Closed | Guard returns/raises before `cancel_order`/`modify_order`; `test_cancel_*_fails_without_mutation` suites assert no broker mutation call |
| 2 | Strict target precedence: conflict → 409; owned-by-run → authorize; owned-elsewhere → 404 STOP; unowned → parent path only then | Closed | `worker_execution.py:309-341`; `test_cancel_target_owned_by_other_run_refused_even_with_owned_parent_and_confirmed_child`, `test_modify_*` |
| 3 | Parent path verifies all elements; caller-supplied parent is never evidence | Closed | `worker_execution.py:318-339`; `test_cancel_caller_lying_about_parent_fails`, `test_cancel_snapshot_returning_different_order_id_refused`, `test_cancel_malformed_snapshot_refused` (failed read → 404, malformed → 404, returned id/parent equality enforced) |
| 4 | Link authoritative; intent fallback; distinct-owner or link-vs-intent disagreement = corruption, fail closed, never `LIMIT 1` | Closed | `_get_live_order_ownership_sync` uses DISTINCT owner sets; `test_cancel_link_intent_disagreement_fails_observably` + PostgreSQL §4 |
| 5 | Ordering: token/run access → hosted attempt authority → live mode → ownership proof → mutation | Closed | Call sites `:602-610` and `:626-634`; `test_cancel_stale_hosted_attempt_refused_before_mutation` (403 `HOSTED_ATTEMPT_UNKNOWN` precedes any ownership evidence) |
| 6 | Non-disclosing refusals: 404 wording identical to read path; corruption distinct + observable (409 `ORDER_OWNERSHIP_CONFLICT`) | Closed | `test_cancel_unknown_and_unowned_share_identical_refusal`, `test_cancel_cross_account_token_non_disclosing_and_refused` |
| 7 | Hosted attempt fencing still applies | Closed | `enforce_hosted_attempt_authority` precedes the guard at both call sites; stale-attempt test above |
| 8 | Live-only; paper unchanged | Closed | `_require_live_run` precedes the guard at both call sites — paper/dry-run can never reach it; no paper-path file touched (`git diff --stat backend/schema.sql` empty; no paper runtime file in any commit) |
| 9 | Legacy unlinked orders stay fail-closed; G1 run binding does not repair order links | Closed (as refusal) | No backfill, no heuristic; unowned target without proofable parent → 404 (`test_cancel_manual_or_unknown_target_fails_without_mutation`); named later work in §6 |

## 2. Acceptance evidence (roadmap "Acceptance evidence" list → tests)

| Required scenario | Test (`tests/api/test_algo_worker_api.py`) | Status |
| --- | --- | --- |
| Direct owned target succeeds | `test_cancel_direct_owned_target_succeeds`, `test_modify_owned_target_succeeds` | Closed |
| Direct cross-run target fails | `test_cancel_direct_cross_run_target_fails_without_mutation`, `test_modify_direct_cross_run_target_fails_without_mutation` | Closed |
| Manual/unattributed target fails | `test_cancel_manual_or_unknown_target_fails_without_mutation` | Closed |
| Owned parent + unrelated target fails | `test_cancel_owned_parent_plus_unrelated_target_fails` | Closed |
| Genuine child with authoritative parent succeeds | `test_cancel_genuine_child_with_authoritative_parent_succeeds` | Closed |
| Caller lying about parent fails | `test_cancel_caller_lying_about_parent_fails` | Closed |
| Unestablishable child relation fails | `test_cancel_child_relation_unestablishable_fails` | Closed |
| Link/intent disagreement observable | `test_cancel_link_intent_disagreement_fails_observably`, `test_modify_link_intent_disagreement_fails_observably` | Closed |
| Stale hosted attempt fails before mutation | `test_cancel_stale_hosted_attempt_refused_before_mutation` | Closed |
| No mutation on any refusal | asserted in every `*_fails_without_mutation` test | Closed |
| Cross-account / unknown non-disclosing | `test_cancel_cross_account_token_non_disclosing_and_refused`, `test_cancel_unknown_and_unowned_share_identical_refusal`, `test_worker_cancel_order_requires_run_access` | Closed |
| Lookup contract exists (repo-level) | `test_get_live_order_ownership_exists_and_fake_honors_contract` | Closed |

PostgreSQL integration (`tests/integration/test_worker_order_ownership_postgres.py`,
new file): migration head `20260915_000024`; link authority / intent fallback /
unowned / cross-account isolation; link-vs-intent disagreement → conflict; two link
owners → conflict; concurrent `upsert_order_link` leaves exactly one durable owner and
the lookup resolves one owner (never two, never silent pick).

## 3. Test commands and results (executed 2026-09-17, this machine)

| Command | Result |
| --- | --- |
| `pytest tests/api/test_algo_worker_api.py -k "ownership or cancel_direct or … or cancel_unknown_and_unowned" -q` | 18 passed |
| `pytest tests/api/test_algo_worker_api.py -q` | 147 passed |
| `pytest tests/api -q` | 522 passed, 20 failed — **failure set byte-identical to pre-G14 baseline `15335c1`** (verified in a detached worktree; diff of sorted FAILED lists empty). Failures are pre-existing environment/config (`test_auth_service_release`, `test_control_plane_api`, `test_public_runtime_config`), none touch worker ownership |
| Hosted/worker neighbour suites (`test_hosted_child_authority`, `test_hosted_lifecycle_api`, `test_algo_worker_route_mounts`, `tests/broker_api/test_worker_execution_links`, `tests/sdk/test_worker_run_access`, `test_worker_runtime_recovery`, `test_worker_safety`) | 50 + suite passes, all green |
| `python -m compileall -q` changed files | clean |
| `git diff --check` | clean |

## 4. PostgreSQL evidence

Server: `kite-test-postgres` (PostgreSQL 16, port 15433), the repo's standard
disposable test server. The fixture creates a uniquely-named throwaway database, runs
`alembic upgrade head` against it (exporting `DATABASE_URL` around the upgrade
because `backend/alembic/env.py` overrides `sqlalchemy.url`), and drops it. Without a
configured URL the suite **skips — never fake-passes on SQLite**.

```
WORKER_OWNERSHIP_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  .venv/bin/pytest tests/integration/test_worker_order_ownership_postgres.py -q
→ 5 passed
```

## 5. Defects found while implementing (plan deviations, all corrected in the commits)

- **D1 (safety-relevant, plan's PG fixture):** `alembic_cfg.set_main_option("sqlalchemy.url", …)` is overridden by `env.py`'s `get_database_url()`; verbatim, the upgrade could have targeted the ambient database. Fixed by exporting `DATABASE_URL` around the upgrade (established pattern in `test_hosted_strategy_foundation_postgres.py`).
- **D2 (plan test would 500 instead of refusing):** `_serialize_model(None)` raises outside the plan's `try`. Fixed: serialization inside the `try`, plus explicit `isinstance(payload, dict)` check.
- **D3 (plan fencing test could not run):** `sqlite:///:memory:` gives each thread its own DB and `strategy_jobs` was unregistered. Fixed with `StaticPool` + `check_same_thread=False` + model import.
- **D4 (plan assertion contradicted known race):** concurrent `upsert_order_link` can raise the documented unique-index race the placement path already swallows; production code left untouched. The test now pins the invariant the guard depends on: exactly one durable owner, lookup resolves one owner, any error must be the known `idx_worker_exec_links_order` race.
- **D5 (selector typo):** plan's `-k AlgoWorkerOrderOwnerLookupTests` did not match the defined class name; corrected in execution.

## 6. Limitations and deferred (explicit)

- Legacy unlinked orders remain fail-closed; no backfill. An audited order-attribution backfill or authoritative broker read proof is separate later work. G1's run binding does not repair order links.
- Broker-side child orders with no platform record are authorizable only via the parent-proof path (C). Persisting child links at placement time is named later work.
- Conflicts are surfaced (409), not repaired; nothing reconciles disagreeing rows.
- The `upsert_order_link` check-then-act race remains (transient unique-violation, logged and swallowed by the placement path). A lost link reads as unowned → fail-closed 404: safe but opaque. `ON CONFLICT DO NOTHING` is a deliberate non-goal for this slice.
- Read endpoints unchanged by design.

## 7. NOT PROVEN (requires separate authorization)

- Behaviour against the real Kite API (real `order_snapshot` payloads, real cancel/modify). All broker interactions in tests are fakes/stubs; live-market certification is a separate campaign step.
- Deployment of the fix to the running stack.

## 8. Gate summary

All roadmap Project 0 invariants and acceptance evidence are Closed with executed
tests, including real-PostgreSQL precedence/conflict/concurrency coverage. Paper/live
certification: **N/A for this phase** (no execution lane added; live behaviour NOT
PROVEN per §7). Phase gate: **PASSED**. Next-phase input recorded in
[hosted-strategies-campaign-ledger.md](hosted-strategies-campaign-ledger.md).
