# Alerts platform — Phase 6 parity matrix (operator UI, canvas, certification)

Status as of 2026-09-13, branch `development`. Phase 6 scope per
[alerts-platform-spec-v2 §9](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md):
6A correctness + operator API + structured UI, 6B visual canvas, 6C certification.
**MCP (Phase 5) remains deferred. Alerts and screeners never place orders.**

> **Read this first.** This document distinguishes states and never blurs them:
> **Closed** (implemented and covered by an executed test in this repo),
> **Verified locally** (executed here, on this machine, with the command shown),
> **NOT PROVEN** (not executed — a live action requiring authorization or isolated
> infrastructure, or a measurement not yet run), and, for the closure table in §8,
> **IMPLEMENTED / PARTIAL / NOT IMPLEMENTED**, plus **VERIFIED LOCALLY / DEPLOYED /
> LIVE VERIFIED / CERTIFICATION PENDING**. A "Closed" row means the *mechanism* is
> closed by automated tests, **not** that live production behaviour has been observed.
>
> **Closure update (2026-09-13, branch `development`).** A later closure pass added
> structured-authoring, operations and canvas work on top of the state this document
> first described. Where a row below is superseded, the authoritative per-requirement
> status is the table in **§8**; the earlier narrative is retained with its revision
> context rather than rewritten to look current.

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Operator API surface (`/api/alerts/*`, cookie auth) | Closed — `backend/api/routers/alerts_operator.py`, `alerts_operator_platform.py`, `alerts_operator.py` tests |
| LTP freshness (E-27) | Closed — `tests/workflows/test_ltp_freshness.py` |
| Failure isolation + supervision | Closed — `tests/workflows/test_failure_isolation.py` |
| Level vs edge clarity (warning severity) | Closed — `tests/workflows/test_level_vs_edge.py` |
| Canvas layout store + namespaced identity | Closed — `tests/workflows/test_canvas_layout.py`, frontend `canvas.test.ts` |
| Structured alerts UI (list/create/detail/edit/universes) | **PARTIAL → expanded** — `frontend-next/features/alerts/`. Basic flow was Closed; advanced structured authoring (boolean groups, hysteresis, consecutive bars) and lossless merge/advanced editor were added in the 2026-09-13 closure. See §8 |
| Screener authoring + inspection | **PARTIAL → expanded** — authoring existed but the edit path refused canonical documents and the schedule range was truncated; corrected in the closure. See §8 |
| Universe targeting in authoring | Closed — `lib/universe-targeting.test.ts`; kind-specific creation added in the closure |
| Ops surface (channels/tokens/producers/health) | **PARTIAL → expanded** — added individual token scopes, producer credential management, durable suppression counters |
| Canonical parity across authoring paths | Closed — `tests/workflows/test_yaml_roundtrip.py` (a note: this suite uses fixture documents, so it did **not** catch that the editor could not open the backend's canonical stored form; the closure added frontend tests for that) |
| Capacity benchmark harness (methodology) | Closed — §3; four fidelity bugs found and fixed, plus a guard that refuses a verdict when nothing fired |
| Capacity target (500 symbols / 5,000 rules) | **NOT PROVEN** — run attempted, did not converge; no supported-capacity figure claimed (§3.4) |
| Live: operator auth boundary, worker scoping, stale-tick rejection, E-27 silent-instrument observability, bounded real send | **Verified live** on the deployed stack (§5.2) |
| Live: UI→API→worker→event, currency session, live restart injection | **NOT PROVEN** — Phase 6 is not deployed (§5.1) and markets were closed (§5.3) |
| Defects found and fixed while certifying | Closed + mutation-verified — §4 (catalog-binding savepoint, ownership first-claim race, emission misclassification) |

---

## 1. E-1 … E-30 matrix

Reused evidence is labelled with its owning phase. New or genuinely changed behaviour is
labelled **6A**. Nothing here re-litigates Phase 4; the Phase 4 parity document remains the
authority for F10.

| E | Scenario | Status | Evidence |
| --- | --- | --- | --- |
| E-1 | Crash between state and outbox | Closed | Phase 1 rollback tests incl. real PG (`tests/integration/test_alerts_phase4_postgres.py`) |
| E-2 | Duplicate occurrence | Closed | Phase 1 occurrence-key loser test; breadth `test_out_of_order_crossing_repeat_and_restart_do_not_duplicate` |
| E-3 | Lease expiry mid-evaluation | Closed | Phase 1 fencing + `test_alerts_postgres_hardening.py` |
| E-4 | Duplicate completed candle | Closed | Phase 1 dedup test |
| E-5 | Worker restart | Closed | Phase 1 new-epoch test + **6A** `test_ltp_freshness.py::… persists continuity invalidation across restart` |
| E-6 | Feed gap | Closed | Phase 1 candle gap + Redis outage rebuild; **6A** LTP silence gap → `ltp_gap` |
| E-7 | Candle correction | Closed | Phase 1 correction audit test |
| E-8 | Session end, no triggering tick | Closed | Phase 1 session-close finalization (`test_session_routing.py`) |
| E-9 | Already true at activation | Closed | Phase 1 activation guard; UI states "a fresh activation is silent by design" |
| E-10 | Baseline drift | Closed | Phase 2 pct-op baseline stored at activation (`test_binding_lifecycle.py`) |
| E-11 | Equality / operator semantics | Closed | Phase 1 operator tables (`test_compiler.py`) |
| E-12 | Same symbol two exchanges | Closed | Phase 2 catalog + session mismatch (`test_session_routing.py`) |
| E-13 | Late / out-of-order | Closed | Phase 4 allowed-lateness + breadth late observation |
| E-14 | Message exceeds limits | Closed | Phase 1 truncation in `build_message`; per-provider limits in capabilities |
| E-15 | Insufficient warmup | Closed | Phase 2 `test_service_warmup.py` |
| E-16 | Forming daily candle | Closed | Phase 2 feature engine completed-only; pair operand `pair_missing` |
| E-17 | Boundary oscillation | Closed | Phase 3 hysteresis tests; **6A** the screener editor warns before save (`screener-authoring.test.ts` exit-band case) |
| E-18 | Partial screener run | Closed | Phase 3 partial coverage path; **6A** UI labels partial and refuses to present it as a membership replacement (`screener-page.tsx`) |
| E-19 | Backlog coalescing | Closed | Phase 3 scheduler tests |
| E-20 | One channel fails | Closed | Phase 1 per-destination failure/retry; delivery history shows per-attempt outcomes |
| E-21 | Provider timeout → unknown | Closed | Phase 1 adapter classification |
| E-22 | Retry after unknown | Closed | Phase 1 unknown-retry budget |
| E-23 | Exactly-once disclosure | Closed | Documentation check: an `accepted` delivery is labelled **"provider accepted"**, never "you received this" (`lib/health.ts` + its test) |
| E-24 | Missing / rotated secret | Closed | Phase 1 test-send 400 `missing_env_secret`; ops UI never displays a secret value |
| E-25 | Notification storm | Closed | Phase 2 delivery budget + admission warming |
| E-26 | Zero denominator | Closed | Phase 2 pair/feature unknown tests |
| E-27 | Illiquid, no ticks all day | **6A — Closed** | `backend/workflows/runtime.py` timer-driven `tick_age_s` from `last_accepted_tick_at`; `tests/workflows/test_ltp_freshness.py` includes a *no-tick observability* case. UI renders staleness with no tick arriving |
| E-28 | Malformed YAML | Closed | Phase 1 validate tests |
| E-29 | Idempotent create/activate | Closed | Phase 1 idempotency tests |
| E-30 | Archived workflow queried | Closed | Phase 1 events retained; **6A** deliveries/attempts retained and shown after archive |

### E-27 detail (the one materially new correctness item)

The failure this closes was real and observed: a **5,760 s** event↔receipt skew (19× the
300 s bound) presented as a live tick, because `RedisTickSource` discarded `received_at` and
`last_trade_time` and nothing compared event time to wall clock. A receipt-age check could not
have caught it — the frozen snapshot carried a *fresh* `received_at`.

| Behaviour | Evidence |
| --- | --- |
| Skew beyond the bound → dropped, `stale_tick`, no state advance | `test_ltp_freshness.py` |
| Fresh `received_at` is insufficient (the actual failure) | `test_ltp_freshness.py` |
| Future timestamp beyond the bound → `future_tick`, dropped | `test_ltp_freshness.py` |
| Silence → `ltp_gap`, continuity invalidation **persisted** (own transaction) | `test_ltp_freshness.py` |
| Recovery tick after a stale interval cannot fabricate a crossing | `test_ltp_freshness.py` |
| Durable bookkeeping survives (a spent `once` rule is not re-armed; cooldown not bypassed) | `test_ltp_freshness.py` |
| Staleness observable with **no tick arriving** | `test_ltp_freshness.py` |
| Kill switch restores prior behaviour | `test_ltp_freshness.py` |

---

## 2. Phase 6 additional certification scenarios

| Scenario | Status | Evidence / method |
| --- | --- | --- |
| Canonical parity: YAML ↔ REST/SDK dict ↔ form-shaped ↔ canvas-shaped | **Verified locally** | `tests/workflows/test_yaml_roundtrip.py` — 28 tests, executed: all shipped fixtures plus a form-shaped universe-targeted alert and a screener with attachments compile to one hash |
| Readable YAML renderer is lossless and idempotent | **Verified locally** | same suite: render → parse → identical hash; second pass byte-identical |
| Cosmetic vs semantic (layout cannot move a hash) | **Verified locally** | `tests/workflows/test_canvas_layout.py` (no revision created) + `test_yaml_roundtrip.py::test_cosmetic_only_change_is_not_in_the_document` |
| Namespaced canvas identities; stage/channel name collision does not cross-assign | **Verified locally** | `features/alerts/lib/canvas.test.ts` — includes the `telegram_primary` stage-vs-channel case |
| Preview side-effect isolation | Closed | Phase 2/4 preview tests; preview persists nothing (the UI states this) |
| Permissions + cross-owner isolation (operator routes) | Closed | `tests/api/test_alerts_operator.py` — 404 for foreign ids, 401 without a session |
| Scope authorization + CSRF/origin | Closed | `tests/api/test_alerts_operator_scope.py`, `test_alerts_operator_csrf.py` |
| Restart / takeover / gaps / notification failure | Closed | Phase 1–4 suites + `test_failure_isolation.py` |
| Supervised restart cleanup (no doubled feed) | Closed | `test_failure_isolation.py` asserts prior sources are stopped and the market-runtime owner released exactly once |
| Required-task liveness in health | Closed | `test_failure_isolation.py` + compose healthcheck predicate |
| Catalog binding absent (pre-migration deployment) | **Verified locally (new)** | `tests/integration/test_alerts_catalog_binding_postgres.py` — PG-only; see §4 |
| Bounded workflow failure isolation | Closed | `test_failure_isolation.py` (live fault injection NOT PROVEN) |
| **Live** frontend → API → worker → event/history run | **NOT PROVEN** | Requires the running stack + a destination; not executed in this environment |
| **Live** stale health with no ticks | **NOT PROVEN** | Requires a live worker pointed at a quiet instrument |
| **Live** currency validation (09:00–17:00 IST) | **NOT PROVEN** | Window-dependent; carried from Phase 4 §12 |
| **Live** controlled production Phase 4 smoke | **NOT PROVEN** | Requires destination authorization + send budget |
| Scheduler-ntfy cutover | **Not started** | Post-certification per spec §9 |

---

## 3. Capacity measurement (500 symbols / 5,000 rules)

**Harness:** `scripts/alerts_capacity_benchmark.py`. It authors, activates and materializes
workflows exactly as production does and feeds observations through
`EvaluationWorker._dispatch` — the same entry point a live tick or completed candle uses.
No component is called directly, so indexing, the ownership fence, the publication
transaction and the outbox are all on the measured path.

### 3.1 Getting the workload right took four corrections

Every one of these was a harness bug that made the measurement either impossible or
flattering. They are recorded because each one is a way the number could have been wrong
without anyone noticing.

| # | Harness bug | Why it mattered |
| --- | --- | --- |
| 1 | **Serial dispatch** | One lane measures `1 / latency`, not the service. Throughput "saturated" at ~75/s with a single lane; the ceiling was the driver's own loop. |
| 2 | **Dispatch per subscription** | A real feed fans one instrument tick out to *every* rule on that symbol. Feeding each rule separately under-sampled every rule by its fan-out factor (10 rules/symbol → a tenth of the ticks), so the workload was not the documented one. Now dispatches per **stream** — an `(instrument, clock)` pair — and fans out. |
| 3 | **Clock keyed by instrument** | An LTP rule and a candle rule on one symbol advance time by different steps (1 s vs 5 min). Sharing one clock per symbol produced non-monotonic timestamps, which the runtime correctly rejected as replays — so nothing was ever evaluated. Now keyed per stream. |
| 4 | **A series that never crossed** | `close = 101 + n % 5` stays *above* the threshold, and a value that never crosses never fires. The whole event + outbox path was unexercised while the latency looked fine. Now the series alternates either side of the threshold. |

Correction 4 is now guarded rather than fixed-and-forgotten: the harness **refuses to print
any verdict** when the run produced zero events, and prints a FIDELITY FAILURE instead. A
measurement that silently excludes the write path is worse than no measurement.

### 3.2 Documented workload

| Dimension | Value |
| --- | --- |
| Rules | 5,000 over 500 symbols (11 workflows, 5,050 subscriptions) |
| Rule mix | 50 % LTP edge, 30 % candle-close edge, 15 % candle + indicator (history read + feature compute), 5 % breadth (workflow-level advisory lock) |
| Input | `--rate` counts **stream ticks** (`(instrument, clock)`), not rule evaluations. One tick fans out to every rule on the stream |
| Feature sharing | One `sma(20)` per indicator workflow, so shared-dependency dedup is exercised |
| Freshness | Event time == receipt time, so the 6A.0 skew bounds never reject the synthetic feed |
| Hardware | 10 cores available; **CPU-core usage is reported per row**, because it decides whether the number describes the service or the driver |

### 3.3 The decisive diagnostic: cores, not latency

Peak CPU across all lanes is reported for every run. It separates the two things a latency
number alone cannot:

| Peak cores | Meaning |
| --- | --- |
| **< ~1.5** | **Driver-bound.** One Python process cannot use more than ~1 core of interpreter work. The ceiling is the harness; the service was never saturated |
| ≥ ~1.5 | The interpreter is not the only limit, so the remaining cost is attributable to the service by comparing mixes |

Measured on this host, 25 symbols / 100 rules:

| Concurrency | Ticks/s | Evals/s | p50 | p95 | p99 | Cores | Events |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 lane | 66.7 | 66.7 | 13.4 ms | 22.7 ms | 28.5 ms | 0.55 | — |
| 2 lanes | 104.3 | 104.3 | 20.6 ms | 37.1 ms | 41.8 ms | 0.84 | — |
| 4 lanes | 130.9 | 130.9 | 26.4 ms | 69.4 ms | 82.8 ms | 0.99 | — |
| 8 lanes | 141.9 | 141.9 | 30.0 ms | 188.6 ms | 241.6 ms | 1.03 | — |
| 16 lanes | 136.6 | 136.6 | 35.1 ms | 485.6 ms | 648.2 ms | 1.04 | — |
| 32 lanes | 128.4 | 128.4 | 37.6 ms | 762.0 ms | 1982.0 ms | 1.03 | — |

**That is GIL saturation, not a service limit.** Throughput stops scaling at ~4 lanes while
latency grows without bound — the signature of many threads queueing for one interpreter, and
the core count confirms it by pinning at ~1.03 regardless of lane count.

### 3.4 Target-workload result

Run on the migrated PostgreSQL database with the corrected harness: 500 symbols / 5,000
rules, 5,050 subscriptions, ~1,000 streams, offered 500 stream ticks/s, 20 s per step.

**The run did not converge, and that is the result.** Across ~10 minutes of wall time the
workload completed roughly **2,200 rule evaluations (~3.6/s)** and produced **no events at
all**, because with ~1,000 streams and a fan-out of ~5 rules per tick, a single lane cannot
give most streams even their *second* observation in a step — and a crossing needs two
consecutive observations. The same workload at 25 symbols / 100 rules reached ~130
evaluations/s (§3.3), so the collapse is a function of stream count and fan-out, not of rule
complexity.

Attempting the run also exposed a cost in the harness itself that must not be read as service
cost: `SyntheticHistory.recent_bars` constructs 30 `Observation` objects on *every* indicator
evaluation. That is harness overhead — a real deployment queries `historical_candles` — and it
sits on the measured path for the 15 % indicator share.

**Conclusion, stated as narrowly as the evidence allows:**

- A **single worker process** — the deployed shape (one container, asyncio tasks) — does not
  come close to the documented 500-symbol / 5,000-rule tick rate on this host. Every
  single-process measurement here lands between ~4 and ~140 rule evaluations/s depending on
  the mix, against an offered load of ~2,500 evaluations/s (500 ticks/s × fan-out ~5).
- The limiting factor is the single interpreter, not the database: CPU pins at ~1.0 core while
  PostgreSQL sits idle, and adding lanes *raises latency* (p95 22 ms → 762 ms from 1 → 32
  lanes) rather than throughput.
- **The service's true capacity is therefore NOT measured**, and this document claims **no
  supported-capacity figure** for the target workload. Two things must change before it can be:
  1. **Driver and evaluator in separate processes.** The harness's driver shares an interpreter
     with the service it is measuring. Either run several driver processes (`--shard`, now
     enforced per-database and mergeable with `--aggregate`) or feed a deployed worker over
     Redis from a load generator — the latter is the real answer, because it separates the two
     exactly as production does.
  2. **A history backend that is not the harness.** Indicator rules must read real
     `historical_candles`, not 30 objects built per call in the driver.

**NOT MEASURED:** Redis pub/sub throughput and delivery fan-out. Delivery fan-out is excluded
by construction — the benchmark workload declares **no channels**, because a channel name that
resolves to nothing violates a foreign key and rolls the whole transaction back (see §4.2B).
That exclusion is deliberate and visible rather than silent.

---

## 4. Defect found and fixed during certification

**`EvaluationService._resolve_catalog_binding` poisoned its caller's transaction on PostgreSQL.**

The method documented that "alert tests and pre-migration deployments continue to materialize
subscriptions without a binding". It caught `CatalogUnavailableError` (raised when the catalog
tables are absent) and returned `None` — correct on SQLite. On PostgreSQL a failed statement
aborts the surrounding transaction *even when the Python exception is handled*, so the caller's
transaction was already dead and the next `INSERT` failed with
`psycopg2.errors.InFailedSqlTransaction: current transaction is aborted`.

| | |
| --- | --- |
| Blast radius | Any database without the instrument catalog tables — i.e. exactly the pre-migration deployment the branch exists to support. SQLite hid it; production has the catalog, so it was latent rather than active |
| Discovery | Found while building the capacity harness, against a database created from ORM metadata only |
| Fix | A `SAVEPOINT` (`session.begin_nested()`) around the catalog lookup, rolled back on failure so the outer transaction survives |
| Regression test | `tests/integration/test_alerts_catalog_binding_postgres.py` (PostgreSQL-gated) |
| Non-vacuity | **Verified by mutation**: reverting the savepoint rollback makes the test fail with `psycopg2.errors.InFailedSqlTransaction` — the exact original symptom |

A second, smaller observation from the same work, recorded because it will bite the next
person: the schema mixes types — `workflows.id` is `text` while `signal_events.workflow_id`
and the Phase 4 tables are `uuid`, so an uncast join raises
`operator does not exist: uuid = text`. Not changed here (it is pre-existing and the fix would
be a migration), but it is why `_cleanup` casts.

### 4.1 Two pre-existing repository defects that blocked certification

Both were found by *running* the suites, and both were proved pre-existing before being
touched (by stashing the relevant change and re-running).

| Defect | Symptom | Fix |
| --- | --- | --- |
| **Date-rot in `tests/screeners/test_universe_screener_kind.py`** | `T0` was pinned to `2026-09-09`; the screener freshness check compares against wall-clock now with a 3-day default limit, so from 2026-09-12 the *happy-path* test failed with "complete run … is stale" and **no code had changed**. | `T0 = now - 1h`, with a comment explaining why a fixed seed cannot work here. The stale-path and extended-limit tests keep their offsets and remain meaningful |
| **Collection error in `tests/integration/test_live_protection_certification.py`** | The covered SDK module was deleted in `914152c`; the import-time load raised `FileNotFoundError`. Because it is a *collection* error, it **aborted the entire `tests/integration` directory**, so every certification run silently lost the rest of the suite — including the Phase 4 PG suites | Module-level `pytest.skip(..., allow_module_level=True)` when the source is absent: the coverage is genuinely gone, and a skip records that rather than hiding it |

Also corrected in the environment (not a code change): the `kite_test` database was one
revision behind at `20260911_000016`, so `delivery_attempts.provider_id` (added in
`e422078`, migration `20260912_000017`) was missing and `test_pre_send_lease_recheck_prevents_stale_completion`
failed with `UndefinedColumn`. Running `alembic upgrade head` on it — the documented
lifecycle — fixed it. Certification runs must migrate the database first; the capacity harness
now checks for this and refuses to run rather than failing deep in the write path.

### 4.2 Two more defects found by running the workload

Both surfaced only because the capacity harness stresses paths the unit suites exercise
one-at-a-time. Both are fixed with PostgreSQL regression tests, and both were **mutation-verified**
(reverting the fix reproduces the exact original failure).

**A. The evaluation ownership fence crashed on a concurrent first claim.**
`claim_evaluation` reads the fence row with `SELECT ... FOR UPDATE` — which locks an existing
row but **locks nothing when the row does not exist**. Two claimers racing for a *brand-new*
subscription therefore both reached the INSERT and one died on the primary key:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
"evaluation_ownership_pkey"
DETAIL:  Key (subscription_id, instrument_key)=(..., NSE:S0001) already exists.
```

Losing that race is the fence *working*. What was wrong is how it failed: the `IntegrityError`
escaped, was logged as `evaluation crashed for subscription …`, and aborted the caller's
transaction. Fixed by inserting inside a savepoint and, on conflict, re-reading the winner's
row and falling through to the ordinary lease comparison (which returns `None` while the lease
is held). SQLite cannot show this — it serialises writers — so the test is PostgreSQL-only.

**B. A failed event write was reported as a benign duplicate.**
`handle_observation` catches `IntegrityError` to implement E-2/E-7 (a racing writer already
committed the occurrence key → the loser skips). But it labelled **every** `IntegrityError` as
`duplicate_occurrence`, and a missing foreign key raises the same class. When an alert named a
channel that resolved to nothing, the delivery insert violated its FK and the consequences were
all silent:

- the event was lost while health reported only `duplicate_occurrence`;
- the branch returns early, so the **checkpoint never advanced** — the rule re-fired and
  re-failed on every subsequent observation, forever;
- the misclassification made a real failure look like correct dedup, so nothing prompted
  anyone to look. This is exactly what the first benchmark runs hit: `emitted: 0`,
  `events: 0`, `suppressed: {duplicate_occurrence: 2}` for a workload that had crossed twice.

Fixed by re-reading the occurrence key after the savepoint rollback: present means it really
was a duplicate; absent means something else failed and it is re-raised. This is a
**read-side-only** change — it cannot turn a real duplicate into an error, because a genuine
duplicate necessarily leaves the row behind. The E-2/E-7 suites still pass unchanged.

| | |
| --- | --- |
| Blast radius | Any integrity failure on the emission path. Production supplies a channel resolver, so the FK case is a misconfiguration rather than the normal path — but the *misreporting* applies to any constraint the emission path can violate |
| Tests | `tests/integration/test_evaluation_ownership_postgres.py` (3 tests), `tests/integration/test_emission_integrity_postgres.py` (1 test) |

**A third observation, not fixed (outside this phase's scope):** the channel *test-send*
(`POST …/notification-channels/{id}/test`) delivers a real message but writes no
`delivery_attempts` row, so a test send leaves no durable audit trail. Reported rather than
changed, since it is deliberate-looking (a test send is not a workflow delivery) but means an
operator cannot later prove a test was run.

### 4.3 A process failure worth recording: my own tests broke the shared database

The two new PostgreSQL suites above initially shipped with `TRUNCATE`/`DELETE FROM workflows`
cleanup. `tests/integration` shares ONE database across every module, so that wiped state under
the sibling suites and made `test_screener_postgres.py` fail — non-deterministically, and
initially in a way that looked like a regression in my backend changes.

Two lessons, both now encoded:

1. **A test may only clean up after itself.** Both suites are now owner-scoped (a per-run owner
   for the emission suite; a key prefix for the ownership suite), and every query they make is
   scoped to their own rows. `list_active_subscriptions()` is global — filtering it by revision
   is required, and was the last bug of this kind.
2. **Isolate before blaming.** Stashing the backend changes and re-running is what separated
   "my fix regressed the fence" from "my test fixtures poisoned the database". Without that
   step the wrong conclusion was one run away — the first stash experiment appeared to convict
   the backend changes, and only a repeat run showed the failure moving.

**Residual fragility, NOT fixed:** the integration suites share one database and are therefore
order- and state-sensitive. Per-module databases (or a per-module schema) would remove the whole
class of problem. Worth doing, but it is a test-infrastructure change beyond this phase's scope.

---

## 5. Live validation (deployed stack, markets closed)

Markets closed for the whole session, so nothing here claims live *market* evaluation. What was
verified is stated precisely, and the blocked items are named.

### 5.1 Deployment currency — Phase 6 is NOT deployed

This is the first finding, and it limits everything else:

| Probe | Result |
| --- | --- |
| `GET /api/worker/workflows/capabilities` | 200, but **`universe_index_source_lists` is ABSENT** — the deployed backend predates this work |
| `kite-frontend-next` image (built 2026-09-11) | `.next/server/app/(app)/` contains `analytics, custom-display, dashboard, journal, options, paper, settings, strategies, trading` — **no `alerts` directory**. The deployed frontend has no alerts UI at all |
| `GET /alerts`, `/alerts/new`, `/alerts/screeners/new`, `/alerts/universes` (port 13000) | 307 → `/login?next=…` — auth middleware fires before route resolution, so this says nothing about whether the route exists; the bundle listing above is the authoritative answer |

**Consequence:** there is no deployed UI→API→worker path to exercise, so the live end-to-end
item cannot be validated until this work is deployed. Everything below is either the deployed
*backend/worker* behaviour or a structural check.

### 5.2 Verified live

| Item | Evidence | Classification |
| --- | --- | --- |
| Operator API auth boundary | `GET /api/alerts/workflows` → **401** with no cookie (route mounted, correctly refusing) | LIVE (deployed backend) |
| Worker token scoping | `GET /api/worker/signals/health` → **403** `"Worker token is not allowed to perform 'signals:read'"` — the Phase 4 decision that `signals:*` is grantable but not default holds in the deployed build | LIVE |
| Silent instrument is observable with **no tick arriving** (E-27) | Activated one zero-channel LTP workflow on `NSE:RELIANCE`; health then read `never_ticked_instruments: 1`, `stale_tick_instruments: 1`, `stale_tick_detail: {"NSE:RELIANCE": null}` while `last_evaluated_at: null` | LIVE |
| Stale ticks are rejected, not evaluated | `rejected_ticks: {"stale_tick": 112, "future_tick": 0, "untimed": 0}` — Redis was still delivering stale ticks and every one was refused by the 300 s bound | LIVE |
| No error storm on a quiet instrument | `evaluation_errors: 0`, `startup_errors: 0`, `subscription_failures: {}`, `quarantined: {}`, all three tasks `alive: true` with `restarts: 0` | LIVE |
| Bounded real notification (authorized) | One `POST /api/worker/notification-channels/{id}/test` → **200 `{"status":"accepted","provider_id":"5"}`**. Note the disclosure is exact: **"accepted"** is labelled *provider accepted*, never "you received this" (E-23) | LIVE, real send |

**One real notification was sent** (the authorization allowed 2–3; the minimum was used). Note
the destination was the **Telegram** channel actually configured (`gold-alert-test-…`), not the
ntfy URL present in the worker env — the ntfy topic is not a configured channel.

### 5.3 Blocked, with the reason

| Item | Why it could not be done |
| --- | --- |
| Live UI → API → worker → event end-to-end | **Phase 6 is not deployed** (§5.1). No alerts route exists in the deployed frontend bundle |
| Live candle / LTP market evaluation | Markets closed. The 112 stale ticks were correctly *rejected*, so no tick could legitimately be evaluated — which is the guard working, not a gap in the test |
| Currency session validation (09:00–17:00 IST) | Requires the window and a bound CDS instrument |
| Supervised-restart feed cleanup under live fault injection | Automated assertions pass; live injection was not performed |
| Scheduler-ntfy cutover | Deliberately post-certification per spec §9 |

**Cleanup:** the smoke workflow was archived (`archived_at 2026-09-13T07:03:00Z`,
`revisions_archived: 1`). Live state restored to 14 workflows / **0 active revisions**, matching
the pre-test condition. No smoke workflow is active.

---

## 6. Deployment and rollback

**Order** (each independently deployable): backend migration (additive, nullable) → backend
code → worker → frontend. The frontend tolerates an older backend and vice versa.

**Rollback:** revert the code/image tag for the affected service. Migrations in this phase are
additive, so no down migration is needed. LTP freshness and isolation are guarded by env
switches (`ALERTS_LTP_FRESHNESS_ENABLED`, quarantine/backoff knobs), so behavioural changes can
be disabled without a redeploy.

**Verification after each deploy:** container health (with the freshness-aware, liveness-aware
check), deployed file hashes vs the tree, `/api/alerts/*` reachable with a cookie and 401
without, worker health counters sane.

**Explicitly NOT PROVEN — do not read the tables above as covering these:**

| Item | Why it is open |
| --- | --- |
| 500-symbol / 5,000-rule capacity | Run requires isolated infrastructure; only a 25-symbol/100-rule partial measurement exists (§3) |
| Currency live validation | Requires an eligible session (09:00–17:00 IST) and a bound CDS instrument |
| Controlled production Phase 4 smoke | Requires an authorized destination and a send budget — no real notification is sent without destination authorization |
| Live end-to-end UI → API → worker → event | Requires the running stack |
| Supervised-restart feed-cleanup under live fault injection | Automated assertions pass; live injection not performed |
| Scheduler-ntfy cutover | Deliberately post-certification |

---

## 6. Executed verification commands

```bash
# backend: alerts platform + workflows + operator API
.venv/bin/python -m pytest tests/workflows tests/api/test_alerts_operator.py \
    tests/api/test_alerts_operator_ops.py tests/api/test_alerts_operator_platform.py -q

# canonical parity across authoring paths
.venv/bin/python -m pytest tests/workflows/test_yaml_roundtrip.py -q

# PostgreSQL-gated: catalog-binding regression (the new defect fix)
ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
    .venv/bin/python -m pytest tests/integration/test_alerts_catalog_binding_postgres.py -q

# frontend: units + components
cd frontend-next && NODE_ENV=test npx vitest run

# capacity harness (partial run; NOT the certified target)
.venv/bin/python scripts/alerts_capacity_benchmark.py \
    --database-url postgresql+psycopg2://…/kite_bench \
    --symbols 25 --rules 100 --rate 40 --duration-s 6
```

**Executed results at the time of writing** (branch `development`, all commands above):

| Suite | Result |
| --- | --- |
| `tests/workflows` + the three operator suites | **500 passed** |
| `tests/screeners` | **60 passed** |
| `tests/integration` (PostgreSQL-gated) | **51 passed, 7 skipped** — stable across 3 consecutive directory runs |
| `tests/integration/test_evaluation_ownership_postgres.py` | **3 passed** (mutation-verified) |
| `tests/integration/test_emission_integrity_postgres.py` | **1 passed** (mutation-verified) |
| `tests/alerts` + `tests/notifications` + `tests/fundamentals` | **251 passed** |
| `tests/workflows/test_yaml_roundtrip.py` | **28 passed** |
| `tests/integration/test_alerts_catalog_binding_postgres.py` | **2 passed** (mutation-verified) |
| frontend `vitest` | **208 passed across 28 files** |
| `tsc --noEmit` / `eslint` on all new files | **clean** |

The pre-existing failure sets (20 `tests/api` control-plane/auth-policy, 11 `broker_api`, one
`test_indicators.py` `__pycache__` collision) are byte-identical with and without this work and
remain documented, not re-litigated. The two pre-existing defects in §4.1 are now **fixed**, so
they no longer appear in that set.

---

## 7. Decisions taken under D6-*

| # | Decision | Taken |
| --- | --- | --- |
| D6-1 | Browser → backend auth | App-cookie operator router; worker boundary untouched |
| D6-1a | Scope authority | Server-side allowlist; `?scope=` is a selection, not an authority |
| D6-1b | Cookie CSRF on HTTPS | Explicit CORS allowlist + same-origin assertion on unsafe methods |
| D6-2 | LTP freshness defaults | 300 s, tunable, kill-switchable |
| D6-3/3a | Where stale handling lives; observability | Service; timer-driven age so silence is observable |
| D6-4 | Canvas library | **Hand-rolled SVG, no new dependency.** The spike was not run as a formal comparison; the zero-dependency path was chosen because the documented subset (DAG render, drag, pan, collapse) is small enough that a library's bundle and license surface were not justified. Recorded as a judgement, not a measurement |
| D6-5/5a/5b | Layout storage / identity / save semantics | Separate table; namespaced `stage:`/`alert:`/`channel:` ids; cosmetic vs semantic split |
| D6-6 | Level-only rules | Warning, not error |
| D6-7/7a/7b | Quarantine durability; restart cleanup; task liveness | In-memory; tear down before replace; explicit task state in health |
| D6-8 | `provider_id` persistence | Additive column (migration `20260912_000017`) |
| D6-9 | Operator channel secrets | Keep `secret_env` name, never a value; one-time reveal for credentials |
| D6-10 | Scope picker | Picker over authorized scopes only |
| D6-11 | 6A.3 screener depth | Full authoring, reusing the condition editor |

### Decisions taken during implementation (flagged for review)

| # | Decision | Rationale | Rejected alternative |
| --- | --- | --- | --- |
| P6-1 | **Universe refs are emitted in the typed `{kind, name}` form only** | One canonical shape, so a doc written by the form and one written by the SDK normalise identically. The parser accepts the shorthand too, and that form is still *read* | Emitting shorthand `{index: Nifty50}` — smaller, but two shapes for one concept |
| P6-2 | **`intersect` is refused by the editor rather than preserved** | There is no control for it; silently dropping a membership restriction on save would silently *widen* the scan | Passing it through untouched (invisible in the UI, so it would change behaviour with no visible cause) |
| P6-3 | **The screener editor emits the parser's own default rank band** | `entry_rank=top_n`, `exit_rank=top_n+max(1, top_n//2)` — pinned in tests against the documented `top_n=10 → 15`. An empty box would imply "no hysteresis" | Omitting the keys and letting the server default them later |
| P6-4 | **Client-side validation duplicates only decidable rules** | Empty names, missing channels, exit ≤ entry, bounds. The compiler remains the authority and its issues are always shown | Reimplementing server validation in TypeScript |
| P6-5 | **`kind` is never sent by the client** | The server infers screener-vs-alert from the document, so a mismatch cannot be expressed | Sending `kind` and risking a document/server disagreement |
| P6-6 | **Benchmark lanes partition subscriptions disjointly** | Two lanes must never evaluate one subscription concurrently — matching production, where one source loop drives one instrument | A shared queue with lanes pulling arbitrarily (would measure a race the runtime cannot produce) |
| P6-7 | **Benchmark refuses to run on SQLite and refuses to certify a sub-target run** | A capacity figure measured on a database with different write behaviour, or extrapolated from a smaller workload, is exactly what spec §10.6 forbids | Convenience default that would produce a misleading PASS |

---

## 8. Phase 6A/6B requirement status (authoritative, 2026-09-13)

This table supersedes the "Closed/PARTIAL" shorthand in §0 for the requirements it
covers. Status is one of **IMPLEMENTED**, **PARTIAL**, **NOT IMPLEMENTED**; where a
statement is about execution it is qualified **VERIFIED LOCALLY**, **DEPLOYED**,
**LIVE VERIFIED**, or **CERTIFICATION PENDING**.

### 6A.2 Structured alert authoring

| Requirement | Status | Code location | Focused evidence |
| --- | --- | --- | --- |
| Instruments/universe → session/clock → conditions → trigger/limits → channels → validate/preview → save/activate | IMPLEMENTED / VERIFIED LOCALLY | `features/alerts/components/alert-wizard.tsx` | `authoring-components.test.tsx`; wizard unit tests |
| Boolean `any`/`not` groups | IMPLEMENTED / VERIFIED LOCALLY | `lib/authoring.ts`, `components/condition-editor.tsx` | `document-to-draft.test.ts`, `authoring.test.ts` |
| Multiple stages and multiple alerts | **NOT IMPLEMENTED** in the structured form (routed to the advanced editor) | `lib/authoring.ts` refusal reasons | advanced editor writes them losslessly |
| Feature stages / layered stage references | **NOT IMPLEMENTED** in the form; visible read-only on the canvas | `lib/canvas.ts` (`readOnly` stage nodes) | `canvas.test.ts` |
| Consecutive completed-bar conditions (`consecutive_bars`) | IMPLEMENTED / VERIFIED LOCALLY | wizard step 3, `buildDocument`/`applyStageFields` | `authoring.test.ts`, `document-to-draft.test.ts` |
| A-then-B sequences | **NOT IMPLEMENTED** (advanced editor) | `lib/authoring.ts` refusal | — |
| Windowed distinct-symbol breadth | **NOT IMPLEMENTED** (advanced editor) | `lib/authoring.ts` refusal | — |
| Pair ratios / relative-strength operands | **NOT IMPLEMENTED** (advanced editor) | `operandFromDocument` returns null → refusal | — |
| External producer/field operands | **NOT IMPLEMENTED** (advanced editor) | `operandFromDocument` | — |
| Constant-threshold hysteresis | IMPLEMENTED / VERIFIED LOCALLY | `Condition.hysteresis`, condition editor | `document-to-draft.test.ts`, `authoring.test.ts` |
| Arithmetic operands | **NOT IMPLEMENTED** (advanced editor) | `operandFromDocument` | — |
| Rearm level/direction | IMPLEMENTED / VERIFIED LOCALLY | wizard step 4, `alertToDocument` | `authoring.test.ts` |
| Capability-driven bounds; server authoritative | IMPLEMENTED | all pickers read `GET /capabilities`; `Validate`/`Preview` always shown | component tests |
| Trigger contract preserved (level vs crossing; explain non-notifying configs) | IMPLEMENTED | `levelOnlyWarning` + `OperatorIssueList` | `authoring.test.ts` |

### 6A.2 Editing and losslessness

| Requirement | Status | Evidence |
| --- | --- | --- |
| `documentToDraft` replaces refusals as controls land (rearm, groups, hysteresis, consecutive bars) | IMPLEMENTED / VERIFIED LOCALLY | `document-to-draft.test.ts` (16 tests) |
| Definitions outside the subset: preserve the document and offer a labelled advanced YAML/JSON editor with server validation + `expected_revision` | IMPLEMENTED / VERIFIED LOCALLY | `components/advanced-definition-editor.tsx`; wired on the alert and screener edit pages |
| A no-op edit preserves canonical semantics and hash | IMPLEMENTED / VERIFIED LOCALLY | `buildDocument(draft, base)` merge pinned by `document-to-draft.test.ts` |
| A meaningful edit uses the revisioned PATCH path | IMPLEMENTED | wizard + advanced editor both send `expected_revision` |
| A 409 preserves unsaved work and offers recovery; never silently overwrites | IMPLEMENTED | wizard conflict state; advanced editor keeps the text |
| **Editor could not open the backend's canonical STORED form** | **FIXED** — canonical list conditions, verbose operands (`params`), `{symbol, exchange}` instruments, empty any/not arrays | `document-to-draft.test.ts`, `screener-authoring.test.ts` |

### 6A.3 Screeners and universes

| Requirement | Status | Evidence |
| --- | --- | --- |
| Ranking, schedules, attachment hysteresis, run history, coverage, partial-result explanation, baselines | IMPLEMENTED | `screener-editor.tsx`, `screener-page.tsx`, `screener-authoring.test.ts` |
| Reuse the richer condition editor | IMPLEMENTED | `ConditionEditor` shared |
| Schedule range = backend range (5m..31d), not a 7-day truncation | IMPLEMENTED / VERIFIED LOCALLY | `durationFromSeconds`, `DURATION_CHOICES`; test pins 5m..31d |
| Create/edit result-change attachments | IMPLEMENTED | `screener-editor.tsx` attachment controls |
| Screener-result universes feeding downstream alerts | IMPLEMENTED | `universes-page.tsx` kind-specific `screener` config |
| Kind-specific universe creation (explicit/index/portfolio/screener); raw JSON as advanced escape | IMPLEMENTED / VERIFIED LOCALLY | `universes-page.tsx` |
| Union/intersection/exclusion without silently widening | **PARTIAL** — union + exclusion are editable; `intersect` is still refused (no control) rather than dropped | `lib/authoring.ts` refusal |
| Owner isolation / session compatibility / partial runs not shown as complete | IMPLEMENTED | `screener-page.tsx`; operator router owner-scoping tests |

### 6A.4 Operations

| Requirement | Status | Evidence |
| --- | --- | --- |
| Worker-token least-privilege presets | IMPLEMENTED | `operations-page.tsx` |
| Individual-scope selection for supported alerts actions | IMPLEMENTED / VERIFIED LOCALLY | token form "advanced" mode; `GET /tokens/presets` |
| Tokens align with the authorized owner scope; no execution capability | IMPLEMENTED | server re-validates; `ALERTS_TOKEN_ACTIONS` excludes execution |
| One-time secret reveal without persistent storage/cache | IMPLEMENTED | `one-time-secret-dialog.tsx`; `create.reset()` |
| Producer typed schema during registration; list/read details | IMPLEMENTED | `operations-page.tsx` |
| Value history with sampled/expired/revoked states | IMPLEMENTED | `signals/values` panel |
| Credential issue (one-time) + revoke incl. token id + non-secret metadata | IMPLEMENTED / VERIFIED LOCALLY | new `GET /signals/producers/{name}/credentials`; `test_alerts_operator_platform.py` |
| Channels list/create/update; test-send explicit; never display secrets | IMPLEMENTED | existing |
| Health: task liveness, restarts/backoff, quarantine, counters, unknown ≠ zero, stale_subscriptions | IMPLEMENTED | `operations-page.tsx`, `workflow-health-panel.tsx` |
| Durable suppression counters exposed (session cap) | IMPLEMENTED / VERIFIED LOCALLY | workflow health `suppressions`; `test_alerts_operator.py::test_workflow_health_exposes_durable_suppression_counters` |
| Warmup progress | **NOT IMPLEMENTED** (backend exposes no live field); UI shows explicit unavailable/unknown | handoff §16 |

### 6B Visual canvas

| Requirement | Status | Evidence |
| --- | --- | --- |
| Namespaced `stage:`/`alert:`/`channel:` identities | IMPLEMENTED | `canvas.test.ts` |
| Layout-only change: layout API, no revision, unchanged hash | IMPLEMENTED | `test_canvas_layout.py`, `canvas-editor.tsx` |
| Semantic change: document PATCH with `expected_revision` | IMPLEMENTED | `canvas-editor.tsx` |
| Add stage/alert nodes; edit props; create/remove connections; delete with dependency validation | IMPLEMENTED / VERIFIED LOCALLY | `canvas.test.ts` (rename rewrite, dependency blocking, cycle detection) |
| Selection + keyboard navigation | IMPLEMENTED | node `role=button`, arrows/Enter/Delete |
| Undo/redo semantic + layout edits | IMPLEMENTED | history stack in `canvas-editor.tsx` |
| Compiler issues at the relevant node | IMPLEMENTED | `issuesByNode` + Validate |
| Compare with the loaded revision before saving | IMPLEMENTED | `documentsEqual` badge |
| Usable viewport (pan/zoom) | IMPLEMENTED | zoom + background pan |
| Unsupported constructs shown read-only, not omitted | IMPLEMENTED / VERIFIED LOCALLY | `canvas.test.ts` advanced nodes |
| Block edits that would damage a hidden dependency | IMPLEMENTED | channel referenced by a screener attachment is blocked |

### Certification / deployment

| Item | Status |
| --- | --- |
| Operator API, LTP freshness, failure isolation, level-vs-edge, canvas layout store (backend) | IMPLEMENTED / VERIFIED LOCALLY (564 backend tests) |
| Frontend implementation | IMPLEMENTED / VERIFIED LOCALLY (245 vitest, production build clean; release-completion pass §10) |
| Phase 6A/6B deployed | **DEPLOYED: NO** — deployment pending (see the release manifest) |
| Frontend release-completion pass (2026-09-15) | IMPLEMENTED / VERIFIED LOCALLY (§10) |
| Live UI → API → worker → event | CERTIFICATION PENDING |
| Capacity (500 symbols / 5,000 rules) | **NOT PROVEN** — no supported-capacity figure claimed (§3) |
| Currency live validation, production smoke, restart fault injection | CERTIFICATION PENDING |
| Scheduler-ntfy cutover | NOT STARTED (post-certification, by design) |

### What remains genuinely unimplemented (not a live-test gap)

- Structured-form authoring of: **sequences**, **breadth**, **feature stages**,
  **pair/producer/arithmetic operands**, and **multiple stages/alerts**. These are
  supported by the backend and are reachable through the **advanced YAML/JSON
  editor**, but they have no purpose-built controls. Reported as implementation
  work.
- Universe `intersect` has no control (refused rather than dropped).
- Live warmup progress (no backend field).

---

## 9. Closure commits (2026-09-13)

| Commit | Contents |
| --- | --- |
| `3a8683d` | Existing certification defect fixes (ownership fence, emission misclassification, catalog SAVEPOINT, capabilities, revisions route, test hygiene) + PG regression tests |
| `06148cf` | Existing frontend implementation + UI primitives (baseline for the closure) |
| `6353eea` | Track the capacity benchmark harness via a narrow ignore exception |
| `e4485b8` | Lossless structured authoring + advanced YAML/JSON editor + rearm |
| `a211480` | Boolean condition groups, constant hysteresis, consecutive bars |
| `ac9c7a8` | Semantic canvas editing over the canonical document |
| `18e1fb3` | Operations: token scopes, producer credentials, durable suppressions (backend + frontend) |
| `09512f2` | Screener canonical parse + schedule range + universe kind controls |

Migration head: `20260912_000018` (unchanged; the closure added no migration).

---

## 10. Frontend release-completion pass (2026-09-15)

A focused pass on `frontend-next/` closed defects found by auditing the pages against the
required user journeys. **No backend code changed; no migration; the migration head is
still `20260912_000018`.** Every item below is IMPLEMENTED / VERIFIED LOCALLY (Vitest +
`tsc --noEmit` + production build), not DEPLOYED and not LIVE VERIFIED.

### 10.1 Defects fixed

| # | Defect (before) | Fix | Evidence |
| --- | --- | --- | --- |
| 1 | Alert **Edit** and **Canvas** pages had no inbound link anywhere, so the editing and canvas journeys were unreachable for alerts | Detail page now links to the correct editor (alert vs screener) and the canvas | `workflow-detail-page.tsx`; build route list |
| 2 | An **archived** workflow still showed Resume (which 409s, because archiving clears the active revision) and Archive | Action row is archived-aware: Activate-only when archived, Pause/Resume by active revision otherwise | `workflow-detail-page.tsx` |
| 3 | Every detail/screener fetch failure rendered as **"not found"**, hiding 403/503/500 | 404 → "not found"; anything else → the structured error, via `alertsErrorMessage` | `workflow-detail-page.tsx`, `screener-page.tsx`, `universe-detail-page.tsx`, screener edit page |
| 4 | The channel test-send's `400 missing_env_secret` (which **names the env var**) was shown as "Bad Request" — `ApiClientError.message` is the status text | New `lib/errors.ts` reads the structured `detail` (string or `{message,error,secret_env}`) and classifies 403/404/503 | `lib/errors.test.ts` |
| 5 | A failed scopes fetch (or a workflows/runs/universes/channels fetch) rendered as an **empty** state on most pages | Shared `AlertsScopeGate` renders loading/error around every alerts route; list/ops/universe/screener panels check `.error` before the empty branch | `alerts-scope-gate.tsx`, list + panels |
| 6 | The `fellBack` "scope not authorized" banner **flashed** while the scopes list was still loading | `fellBack` is gated on the scopes query having resolved without error | `use-alerts-queries.ts` |
| 7 | Screener runs table read `started_at`/`finished_at`/`member_count`, **none of which the API returns**, so three columns were permanently blank | Types now mirror `RunOut`; columns show `created_at`/`completed_at`/`triggered_by` | `types.ts`, `screener-page.tsx` |
| 8 | Screener **coverage**, **data_freshness**, member **values**, and the runs `note` (the partial-run caveat) were fetched and never shown | All rendered on the run row / member table | `screener-page.tsx` |
| 9 | A manually triggered run that returned `already_finalized` gave **no feedback** | Explicit "already exists; no second scan" message | `screener-page.tsx` |
| 10 | `running` runs never polled, so "run completeness" was not observable without a refresh | Runs/run-detail poll every 5 s **only while a run is running** | `use-alerts-queries.ts` |
| 11 | A failed run-members fetch rendered **nothing**; a failed runs/universes/revisions/destinations fetch rendered an empty list | Each now has an explicit error state | `screener-page.tsx`, `universes-page.tsx`, `universe-detail-page.tsx`, `operations-page.tsx` |
| 12 | Platform health rendered an **absent** counter as `0` and an absent freshness flag as `"off"` | Absent ⇒ "unknown" / "not reported" | `operations-page.tsx` |
| 13 | Per-producer health (`signals/health.producers`) and producer/credential mutation errors were silently dropped | Producer status list and mutation errors rendered | `operations-page.tsx` |
| 14 | A subscription with `stale: true` but a null `stale_reason` rendered green **"flowing"** | `stale` is authoritative; the reason is only the explanation | `workflow-health-panel.tsx` |
| 15 | Opening a stored advanced workflow could **silently drop** fields the form does not model: unknown condition/hysteresis keys were accepted-and-trimmed, and shorthand operands skipped the unmodeled-attribute guard | `documentToDraft` now **refuses** (routing to the advanced editor) instead of dropping | `authoring.ts`; `document-to-draft.test.ts` |
| 16 | The inline level-only warning ignored the `any`/`not` groups and warned for `reminder`/`notify_if_already_true`, diverging from the compiler | Mirrors the compiler: all groups considered; `reminder`, `notify_if_already_true`, `consecutive_bars` suppress it | `authoring.ts`; `authoring.test.ts` |
| 17 | The wizard's session↔instrument check only gated step 0 and never blocked **save**, so an invalid pair could be POSTed | Save is blocked with an explicit reason; instrument-count max enforced | `alert-wizard.tsx` |
| 18 | `preview.issues` were fetched and never shown | Rendered in the preview card | `alert-wizard.tsx` |
| 19 | Switching the advanced editor YAML⇄JSON **discarded unsaved text** | Separate buffer per mode; YAML disabled when the server rendered none | `advanced-definition-editor.tsx` |
| 20 | A stored schedule interval the fixed list did not name (e.g. `2h`) rendered an **empty** Select | The loaded value is shown as a "(current)" option | `screener-editor.tsx` |

### 10.2 Checks executed

| Check | Result |
| --- | --- |
| `npx tsc --noEmit` | clean |
| `npx vitest run` | **245 passed / 29 files** |
| `npx eslint features/alerts app/(app)/alerts` | clean |
| `npm run build` | compiled successfully; all 12 `/alerts/*` routes present |
| `npx eslint .` (repo) | 3 pre-existing errors in **unrelated** files (`components/bottom-dock.tsx`, `components/workspace/workspace-provider.tsx`) — not touched by this work |

### 10.3 Deployment and live verification (explicitly separated)

- **Deployment: PENDING.** The running `kite-frontend-next` image is stale — its server bundle
  contains no `alerts` directory — and the API reports `/api/alerts/workflows` → **401**
  without a cookie. No frontend or backend change from this pass is deployed.
- **Live UI → API → worker → event: NOT PROVEN.** No authorized session was available in this
  environment (only a password *hash* is configured), so the authenticated browser journeys
  could not be exercised. This is a live-verification gap, not an implementation gap.

### 10.4 Bounded list of live acceptance checks still required

1. Sign in and load `/alerts`: list, scope picker, empty/error states.
2. Create an alert through the wizard and open it via the new **Edit**/**Canvas** links.
3. Open an advanced stored workflow and confirm it routes to the advanced editor (no silent drop).
4. Trigger a screener run and watch it move `running → complete|partial` without a refresh; open members, coverage and baselines.
5. Confirm a channel **test-send** with an unset `secret_env` shows the variable name (400 `missing_env_secret`).
6. Confirm `/alerts/operations` platform health reads **unknown** (not 0) when the worker health file is unreadable, and the real counters when it is.
7. One live UI → API → worker → event run, and one live stale-health-with-no-ticks observation (carried from §2/§5).
