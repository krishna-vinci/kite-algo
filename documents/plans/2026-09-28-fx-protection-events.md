# Event-Driven Protection Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Steps use checkbox (`- [ ]`) syntax. Do not load brainstorming or other workflow skills. If a step does not fit the real code, stop at that step and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** A protection breach reaches an exit order in well under a second, and each stage of a staged exit follows the previous stage's fill immediately — instead of today's 5 s poll + serial scan + 60 s claim throttle.

**Architecture:** `MarketDataRuntime` (finance-app's single Redis tick/order-update bridge) gains listener hooks. A new `ProtectionScheduler` maps tokens → protected runs, and on a tick (debounced 250 ms per run) or an order update evaluates just those runs through `WorkerProtectionRuntime`, under a per-run `asyncio.Lock`. The existing 5 s loop stays as a watchdog and now evaluates runs concurrently. The fixed 60 s claim throttle becomes a short in-flight grace; a staged exit in progress always continues even when the metric verdict has cleared. Protection reads ticks from the in-memory `latest_ticks` instead of one Redis GET per token, and persists metric-only changes at most every 5 s.

**Tech Stack:** Python 3.11 asyncio, SQLAlchemy, unittest `IsolatedAsyncioTestCase`, pytest.

**Spec:** Owner decision 2026-09-28 ("protection reacts slowly is deadly"); sol options audit row "Protection latency"; Explore facts (protection_runtime.py:42-1039, market_runtime_client.py:105-378, staged_exit.py:546-1219, background.py:123-189).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-protection`, branch `codex/fx-protection`. Read `AGENTS.md` first.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root. PG only `127.0.0.1:15433`.
- Never touch `.env*`. Fake broker only. No migrations. Do NOT commit. "Checkpoint" = `git status`.
- **Safety invariants that must hold after every task:** (1) one exit claim per run at a time (DB CAS on `generation`/`exit_claim_id` stays the authority); (2) no evaluation of the same run runs concurrently in-process; (3) a listener exception never breaks the tick/order-update loops; (4) the 5 s watchdog still evaluates every run even if no ticks arrive.
- High-risk area: at the end run the whole `tests/sdk/test_worker_protection_runtime.py`, `tests/sdk/test_worker_protection.py`, `tests/broker_api/test_market_runtime_client.py`, `tests/api/test_control_plane_protection.py`, `tests/options -q -k protection`, and the PG file `tests/integration/test_option_protection_ownership_postgres.py`.

## Decisions (fixed)

1. Debounce: `PROTECTION_TICK_DEBOUNCE_MS` env, default 250. A tick for a run schedules one evaluation 250 ms later; further ticks inside that window are coalesced. A tick arriving while that run is being evaluated marks it dirty → exactly one re-evaluation after.
2. Claim throttle: `_has_recent_exit_claim` returns True only while the claim is younger than `PROTECTION_CLAIM_INFLIGHT_SECONDS` (env, default 2.0) — the window in which a submission from the claim may still be in flight. Double-send safety comes from the DB claim CAS and the pre-send fence (`live_order_intents` idempotency), not from a 60 s wait.
3. A run whose state shows an exit in progress (`exit_claim_id` set and `exit_submitted` false, or `exit_submission_status` in `("staging", "partial")` — use whatever the code actually writes for a partially completed staged exit; read `_submit_structure_exit` and report the exact values) always continues its exit, even when the latest metric verdict is no longer triggered. Exits are never abandoned mid-way.
4. Watchdog: the existing loop keeps `WORKER_PROTECTION_INTERVAL_SECONDS` (default 5); `evaluate_once` now evaluates runs concurrently, max `PROTECTION_MAX_CONCURRENCY` (env, default 8), each under its run lock.
5. Metric-only persistence: when a pass changes nothing but metric values/`as_of` (same `status`, `generation`, `triggered_rule`, `exit_claim_id`, `exit_submitted`, `exit_submission_status`, and no timeline events), persist at most once per `PROTECTION_METRICS_PERSIST_SECONDS` (env, default 5) per run.
6. Order updates: on any order update, schedule evaluation for every run whose last known state has an exit claim (the "exit in flight" set, kept in the scheduler from evaluation results). No order-id → run mapping needed.
7. Tick source for protection: in-memory `MarketDataRuntime.latest_ticks` when the runtime is available (finance-app), else the existing Redis GET loader.
8. Latency evidence: the scheduler stamps the tick receive time for the run; when that evaluation submits an exit, record `breach_to_submit_ms` in the heartbeat meta (`last_breach_to_submit_ms`) and log it at INFO. This number decides whether a Go breach detector is needed.

## File Structure

- Modify `backend/broker_api/orders/market_runtime_client.py` — `add_tick_listener`, `add_order_update_listener`, safe dispatch.
- Create `backend/api/services/protection_scheduler.py` — `ProtectionScheduler`.
- Modify `backend/api/services/protection_runtime.py` — `run_key()`, `evaluate_runs(keys)`, per-run locks, concurrent `evaluate_once`, `run_tokens()` per run, claim grace, continue-in-progress-exit, metric persist throttle, `last_evaluation_outcome` for the scheduler.
- Modify `backend/app/background.py` `_worker_protection_loop` — in-memory tick loaders, scheduler wiring, latency meta.
- Tests: `tests/broker_api/test_market_runtime_client.py`, new `tests/api/test_protection_scheduler.py`, `tests/sdk/test_worker_protection_runtime.py`.
- Docs: `documents/kite-algo-platform-reference.md` (protection cadence lines).

---

### Task 1: Listener hooks on `MarketDataRuntime`

**Files:** `backend/broker_api/orders/market_runtime_client.py` (class at :105, `__init__` ~:106-121, `_handle_tick_message` :312-318, `_handle_order_update_message` :342-378); test `tests/broker_api/test_market_runtime_client.py` (`MarketDataRuntimeTests`, `self.runtime = MarketDataRuntime()`).

**Interfaces — Produces:**
- `MarketDataRuntime.add_tick_listener(callback: Callable[[int, Dict[str, Any]], None]) -> Callable[[], None]` (returns an unsubscribe function)
- `MarketDataRuntime.add_order_update_listener(callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]`
- Listeners are called synchronously, after `latest_ticks[token] = tick` / after `ingest_ws_event`, must be cheap (they only schedule work), and any exception is logged (`logger.warning(..., exc_info=True)`) and swallowed.

- [ ] **Step 1: Failing tests** (add to `MarketDataRuntimeTests`; follow how the existing tests there drive `_handle_tick_message` — if it is async, use `asyncio.run(...)` exactly as neighbouring tests do):

```python
    def test_tick_listeners_receive_ticks_and_a_failing_listener_is_isolated(self):
        seen = []

        def boom(_token, _tick):
            raise RuntimeError("listener bug")

        self.runtime.add_tick_listener(boom)
        remove = self.runtime.add_tick_listener(lambda token, tick: seen.append((token, tick.get("last_price"))))
        asyncio.run(self.runtime._handle_tick_message({"instrument_token": 256265, "last_price": 25000.5}))
        self.assertEqual(seen, [(256265, 25000.5)])
        self.assertEqual(self.runtime.latest_ticks[256265]["last_price"], 25000.5)
        remove()
        asyncio.run(self.runtime._handle_tick_message({"instrument_token": 256265, "last_price": 25001.0}))
        self.assertEqual(len(seen), 1)

    def test_order_update_listeners_are_called(self):
        seen = []
        self.runtime.add_order_update_listener(lambda update: seen.append(update.get("order_id")))
        # Patch the ingest/publish side effects exactly as the existing order-update test does.
        ...
```
  For the order-update test, copy the patching of `order_event_runtime.ingest_ws_event` and `publish_event` from the existing order-update test in this file (search `_handle_order_update_message`); if there is none, patch both with `unittest.mock.patch` on the module path `backend.broker_api.orders.market_runtime_client.<name>` and assert the listener got the normalized payload's `order_id`.

- [ ] **Step 2: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/broker_api/test_market_runtime_client.py -q -k listener` → FAIL (`AttributeError: add_tick_listener`).

- [ ] **Step 3: Implement.** In `__init__` add `self._tick_listeners: List[Callable[[int, Dict[str, Any]], None]] = []` and `self._order_update_listeners: List[Callable[[Dict[str, Any]], None]] = []`. Add:

```python
    def add_tick_listener(self, callback: Callable[[int, Dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``callback(token, tick)`` for every tick, after the cache update.

        Listeners run inline on the tick loop, so they must only schedule work.
        A failing listener is logged and never breaks the loop.
        """
        self._tick_listeners.append(callback)
        return lambda: self._tick_listeners.remove(callback) if callback in self._tick_listeners else None

    def add_order_update_listener(self, callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``callback(update)`` for every broker order update, after ingestion."""
        self._order_update_listeners.append(callback)
        return lambda: self._order_update_listeners.remove(callback) if callback in self._order_update_listeners else None

    @staticmethod
    def _notify(listeners: List[Callable[..., None]], *args: Any) -> None:
        for listener in list(listeners):
            try:
                listener(*args)
            except Exception:  # noqa: BLE001 - a listener never breaks the feed
                logger.warning("market runtime listener failed", exc_info=True)
```
  Call `self._notify(self._tick_listeners, token, tick)` at the end of `_handle_tick_message` (after `latest_ticks[token] = tick`), and `self._notify(self._order_update_listeners, <normalized payload dict>)` in `_handle_order_update_message` after `ingest_ws_event` (use the same normalized dict passed to ingestion). Ensure `logger`, `Callable`, `List` are imported/defined in the module.

- [ ] **Step 4: Run** the whole `tests/broker_api/test_market_runtime_client.py` → PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 2: Runtime API for targeted, concurrent, locked evaluation

**Files:** `backend/api/services/protection_runtime.py` (`WorkerProtectionRuntime` :42; `evaluate_once` :96-135; token collection :805-843); test `tests/sdk/test_worker_protection_runtime.py` (fakes `_Repo`, `_OwnerRowRepo`, `_Clock`; class `WorkerProtectionRuntimeTests`).

**Interfaces — Produces:**
- `run_key(run: Mapping[str, Any]) -> str` (module function) = `str(run.get("id") or run.get("strategy_run_id") or "")` — use the SAME identity `evaluate_once` dedups on (`strategy_run_id`); read the code and pick the field that uniquely identifies a pending entry; state it in your report.
- `WorkerProtectionRuntime.pending_runs() -> list[dict]` — the exact list `evaluate_once` builds today (lines 100-123), factored out unchanged.
- `WorkerProtectionRuntime.evaluate_runs(keys: Optional[Iterable[str]] = None) -> Dict[str, int]` — evaluate the pending runs whose `run_key` is in `keys` (all when `None`) concurrently (semaphore `PROTECTION_MAX_CONCURRENCY`, default 8), each under `self._lock_for(key)`; returns `{"evaluated", "triggered", "errors"}` like today. `evaluate_once()` becomes `return await self.evaluate_runs(None)`.
- `WorkerProtectionRuntime.run_tokens() -> Dict[str, set[int]]` — per run key, the tokens `collect_option_metric_subscription_tokens` would add for that run (refactor that method to build this dict and keep returning the union from the old method).
- `WorkerProtectionRuntime.exit_in_flight_keys() -> set[str]` — keys whose last evaluated state had `exit_claim_id` set and was not terminal (maintained from each `_evaluate_run` pass; read the state dict after evaluation).

- [ ] **Step 1: Failing tests** (add to `WorkerProtectionRuntimeTests`, reusing its fakes/constructor pattern from the nearest existing test):
  - `test_evaluate_runs_only_touches_the_requested_runs`: two protection-enabled runs in `_Repo`, `pnl_loader` AsyncMock records calls; `await runtime.evaluate_runs({key_a})` → only run A loaded.
  - `test_the_same_run_is_never_evaluated_concurrently`: `pnl_loader` for run A awaits an `asyncio.Event`; start two `evaluate_runs({key_a})` tasks; assert `pnl_loader` call count is 1 while the event is unset, then set it and await both; total calls 2 (second ran after the first).
  - `test_evaluate_once_runs_runs_concurrently`: two runs whose `pnl_loader` each awaits `asyncio.sleep(0.2)`; `evaluate_once()` completes in < 0.35 s.

- [ ] **Step 2: Run** `-k "evaluate_runs or concurrently or never_evaluated"` → FAIL.

- [ ] **Step 3: Implement** as specified in Interfaces. Per-run locks: `self._locks: Dict[str, asyncio.Lock] = {}` and

```python
    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock
```
  Concurrency: `asyncio.Semaphore(int(os.getenv("PROTECTION_MAX_CONCURRENCY", "8")))` created in `__init__`; gather with `return_exceptions=False` but each task wraps its own try/except exactly like today's loop body (errors counted, `_persist_run_error` called).

- [ ] **Step 4: Run** the whole `tests/sdk/test_worker_protection_runtime.py` → PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 3: Short in-flight grace; never abandon an exit in progress

**Files:** `protection_runtime.py` `_has_recent_exit_claim` (:977-989), its caller (:346), `_evaluate_option_owner_run` early return (~:620); tests in `tests/sdk/test_worker_protection_runtime.py` (existing staging test ~:760-800 uses `clock.advance(180)`).

- [ ] **Step 1: Read** `_submit_structure_exit` (~:1162) and the partial-stage persist path; write down in your report the exact state fields/values that mean "staged exit partially done, continue" (Decision 3).

- [ ] **Step 2: Failing tests:**
  - `test_a_staged_exit_continues_on_the_next_pass_without_a_long_wait`: copy the existing staging test (~:760), but advance the clock by `PROTECTION_CLAIM_INFLIGHT_SECONDS + 0.5` (i.e. 2.5 s) instead of 180 s → second stage submitted.
  - `test_a_fresh_claim_is_not_re_submitted_inside_the_inflight_grace`: claim taken, advance 1 s, evaluate → no second submission.
  - `test_an_exit_in_progress_continues_after_the_metric_clears`: option-owner run with a partial staged exit; the metric loader now returns a non-breaching value; evaluate → the structure exit submitter is still called (next stage).

- [ ] **Step 3: Run** → FAIL (first and third).

- [ ] **Step 4: Implement.**
  - Module constant/env read: `def _claim_inflight_seconds() -> float: return max(0.0, float(os.getenv("PROTECTION_CLAIM_INFLIGHT_SECONDS", "2.0")))`; replace the literal `< 60` with `< _claim_inflight_seconds()`. Update its docstring: the grace covers a submission that may still be in flight; duplicate sends are prevented by the claim CAS and the pre-send fence.
  - In `_evaluate_option_owner_run`, before the "not triggered → return False" early exit (~:620), if the run's state shows an exit in progress (Step 1 fields), skip the verdict gate and continue to the structure-exit continuation path (the same call the triggered path makes, with the SAME existing claim — do not take a new claim). Add a comment: an exit is never abandoned mid-way.
  - Keep the existing `clock.advance(180)` test passing (180 > 2).

- [ ] **Step 5: Run** the whole file → PASS.
- [ ] **Step 6: Checkpoint.**

---

### Task 4: Metric-only persistence throttle

**Files:** `protection_runtime.py` `_persist_state` (:995-1039) and the option-owner metric update path (`update_protection_metrics`, `_record_option_metric_availability` inside `_evaluate_option_owner_run`); test file as above.

- [ ] **Step 1: Failing test** `test_metric_only_changes_are_persisted_at_most_every_five_seconds`: option-owner run, no breach; evaluate 3 times with clock +1 s each → repo persist count 1; advance 5 s, evaluate → 2. Then a breach (status change) persists immediately regardless.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** In `WorkerProtectionRuntime`, keep `self._last_persist: Dict[str, tuple[datetime, tuple]]` = (time, significant-key tuple) per run key, where the significant tuple is `(status, generation, triggered_rule, exit_claim_id, exit_submitted, exit_submission_status)` read from the new state. In `_persist_state`, when there are no `timeline_events` and the significant tuple equals the last persisted one and `now - last_time < PROTECTION_METRICS_PERSIST_SECONDS` (env, default 5) → skip the DB write and return the in-memory state as if persisted (the same return shape the caller expects; read the caller). Otherwise write and update `_last_persist`. Never skip a write that changes any significant field.

- [ ] **Step 4: Run** the whole file → PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 5: `ProtectionScheduler`

**Files:** create `backend/api/services/protection_scheduler.py`; create `tests/api/test_protection_scheduler.py`.

**Interfaces:**
- Consumes: `WorkerProtectionRuntime.evaluate_runs`, `.run_tokens`, `.exit_in_flight_keys` (Task 2); listener hooks (Task 1).
- Produces:

```python
class ProtectionScheduler:
    def __init__(self, runtime, *, debounce_ms: float | None = None, clock=time.monotonic) -> None: ...
    def refresh_tokens(self) -> None            # rebuild token -> {run_key} from runtime.run_tokens()
    def on_tick(self, token: int, tick: dict) -> None       # listener: schedule runs mapped to token
    def on_order_update(self, update: dict) -> None         # listener: schedule runtime.exit_in_flight_keys()
    def schedule(self, key: str, *, received_at: float | None = None) -> None
    async def drain(self) -> None               # test helper: await all pending evaluations
    last_breach_to_submit_ms: float | None
```
  Behaviour: `schedule` records `received_at` (first one wins within a window) and, if no task is pending for `key`, creates `asyncio.create_task(self._run_after_debounce(key))`. `_run_after_debounce` sleeps `debounce_ms/1000`, then calls `await runtime.evaluate_runs({key})`; if `key` was re-scheduled while evaluating (dirty flag), runs once more (no debounce). If the result reports `triggered >= 1`, set `last_breach_to_submit_ms = (clock() - received_at) * 1000` and log INFO `protection breach_to_submit_ms=%.0f run=%s`. Exceptions inside the task are logged, never raised. `on_tick`/`on_order_update` must be non-blocking (they only call `schedule`) and must work when called from the event-loop thread (they are, via the tick loop).

- [ ] **Step 1: Failing tests** (`unittest.IsolatedAsyncioTestCase`, a fake runtime recording `evaluate_runs` calls with an async method and returning `{"evaluated": 1, "triggered": 0, "errors": 0}`, `run_tokens()` → `{"run-a": {1, 2}, "run-b": {3}}`, `exit_in_flight_keys()` → `{"run-b"}`):
  - ticks for token 1 three times within 50 ms with `debounce_ms=100` → after `drain()`, exactly one `evaluate_runs({"run-a"})`.
  - a tick for token 3 → `{"run-b"}` only; a tick for unknown token 99 → nothing.
  - `on_order_update({...})` → `evaluate_runs({"run-b"})`.
  - a tick arriving while run-a's evaluation is running (fake awaits an event) → exactly one more evaluation after it finishes.
  - fake returns `triggered: 1` → `last_breach_to_submit_ms` is a non-negative number.
  - a fake `evaluate_runs` that raises → no exception escapes `drain()`.

- [ ] **Step 2: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api/test_protection_scheduler.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** the module per Interfaces (≈120 lines, docstring explaining: ticks/order updates trigger; the 5 s loop is the watchdog; the scheduler never submits anything itself).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 6: Wire it in finance-app

**Files:** `backend/app/background.py` `_worker_protection_loop` (:123-189).

- [ ] **Step 1: Implement.**
  - In-memory tick loaders: when `getattr(app.state, "market_data_runtime", None)` exists, pass `index_tick_loader` and `option_tick_loader` async callables that return `runtime.latest_ticks.get(int(token))` and fall back to the existing Redis loader (`WorkerProtectionRuntime`'s default) when the cache has no entry. Read the default loader's signature (:905-917) and match it exactly.
  - After constructing `runtime`: `scheduler = ProtectionScheduler(runtime)`; `scheduler.refresh_tokens()`; register `market_data_runtime.add_tick_listener(scheduler.on_tick)` and `add_order_update_listener(scheduler.on_order_update)` when the runtime exists (keep the returned unsubscribe callables and call them in the `CancelledError` branch).
  - Watchdog loop: after each `evaluate_once()`, call `scheduler.refresh_tokens()` and add `"last_breach_to_submit_ms": scheduler.last_breach_to_submit_ms` to the heartbeat meta.
- [ ] **Step 2: Test** — there is no unit test for this function; verify by import + a smoke test: `/home/krishna/kite-algo/.venv/bin/python -c "import backend.api.routers, backend.app.background"` succeeds. Then run the high-risk set from Global Constraints.
- [ ] **Step 3: Checkpoint.**

---

### Task 7: Docs + final run

- [ ] **Step 1:** In `documents/kite-algo-platform-reference.md`, update the protection cadence lines (`grep -n -i "protection" documents/kite-algo-platform-reference.md | grep -i "5 s\|interval\|second\|cadence"`): tick- and order-update-driven evaluation (250 ms debounce) with a 5 s watchdog, concurrent per-run-locked evaluation, 2 s in-flight claim grace, exits in progress always continue, metric-only persistence ≤ every 5 s, `last_breach_to_submit_ms` in the `worker_protection` heartbeat. Add the new env knobs.
- [ ] **Step 2: Final run** — every command in Global Constraints' high-risk list, plus:
```bash
ADMISSION_PG_URL=postgresql://postgres:testonly@127.0.0.1:15433/kite_test RECONCILIATION_PG_ADMIN=postgresql://postgres:testonly@127.0.0.1:15433/postgres /home/krishna/kite-algo/.venv/bin/python -m pytest tests/integration/test_option_protection_ownership_postgres.py tests/integration/test_hosted_live_phase2b_protection_postgres.py -q
```
  (read each PG file's header for its exact env var). Report pre-existing failures (also failing on `development`) separately with test id + error line.
- [ ] **Step 3: Final report** per AGENTS.md, including: the exact "exit in progress" state fields (Task 3 Step 1) and the run-key field chosen (Task 2).
