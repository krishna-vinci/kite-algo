# Tick-Driven Option Chain Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. If a step does not fit the real code, stop and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** Each option-chain session recomputes when its own contracts tick (at most once per `OPTIONS_CHAIN_MIN_INTERVAL_S`, default 1.0 s) instead of on a blind 5 s timer, with the 5 s cadence kept as the ceiling when nothing ticks. Chain freshness goes from ≤5 s to ≤~1 s during trading, with no extra work when the market is quiet.

**Prerequisite:** `codex/fx-protection` is merged into `development` (it adds `MarketDataRuntime.add_tick_listener(callback) -> unsubscribe`). Verify `grep -n "def add_tick_listener" backend/broker_api/orders/market_runtime_client.py` before starting; stop if missing.

**Architecture:** `OptionsSessionManager` registers ONE tick listener on its `market_data` (the same `MarketDataRuntime`, `options_sessions.py:871-874`) and routes each token to the session(s) whose `desired_tokens` contain it, setting that session's `asyncio.Event` "dirty". `OptionsSession._run_cadence` (:255-290) waits for `dirty` or the cadence timeout, whichever first, then enforces the minimum interval since the last compute, then computes and publishes as today. When `market_data` has no `add_tick_listener` (tests' fakes), behaviour is exactly today's timer.

**Tech Stack:** Python 3.11 asyncio, pytest.

**Spec:** Owner decision 2026-09-28; Explore facts (options_sessions.py: `start` :119-155, `_run_cadence` :255-290, `_compute_and_publish` :292-328, `_run_computation` reads `latest_ticks`; manager `__init__` :871-874; `on_ticks` :1057-1061 is a legacy no-op; tests in tests/options/test_options_sessions.py never exercise `_run_cadence`).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-chain-ticks`, branch `codex/fx-chain-ticks`. Read `AGENTS.md` first.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root. Never touch `.env*`. Do NOT commit.
- The listener must be O(1) per tick (a dict lookup + `Event.set()`), never compute on the tick loop.

## Decisions (fixed)

1. `OPTIONS_CHAIN_MIN_INTERVAL_S` env, default 1.0, floor 0.25.
2. The 60 s `_refresh_expiries()` check and error handling in `_run_cadence` stay as they are.
3. Token → sessions map is rebuilt whenever a session's `desired_tokens` changes (after each compute) — keep `self._token_sessions: Dict[int, set[str]]` on the manager, updated in `on_session_update` before `_converge_subscriptions`.
4. The legacy no-op `on_ticks` stays (not used).

## Tasks

### Task 1: Manager listener + token routing
- `OptionsSessionManager.__init__`: if `hasattr(market_data, "add_tick_listener")`, register `self._on_tick` and keep the unsubscribe callable (call it in a new `close()`; if a manager shutdown method exists, call from there).
- `_on_tick(token, tick)`: for each underlying in `self._token_sessions.get(int(token), ())`, `self.sessions[u].mark_dirty()`.
- `on_session_update`: after publish, rebuild entries for `session.underlying` from `session.desired_tokens`.
- Tests (tests/options/test_options_sessions.py): a fake market data with `add_tick_listener` capturing the callback; after one `on_session_update` for a stub session with `desired_tokens={10, 11}`, calling the captured callback with token 10 marks that session dirty; token 99 marks nothing.

### Task 2: Session waits for dirty or cadence
- `OptionsSession.__init__`: `self._dirty = asyncio.Event()` (create lazily inside the running loop if the constructor runs outside one — check how sessions are constructed), `self._last_compute_monotonic = 0.0`; method `mark_dirty()` sets it.
- `_run_cadence`: replace the fixed sleep with:
```python
try:
    await asyncio.wait_for(self._dirty.wait(), timeout=self.cadence_sec)
except asyncio.TimeoutError:
    pass
self._dirty.clear()
min_gap = max(0.25, float(os.getenv("OPTIONS_CHAIN_MIN_INTERVAL_S", "1.0")))
wait = min_gap - (time.monotonic() - self._last_compute_monotonic)
if wait > 0:
    await asyncio.sleep(wait)
await self._compute_and_publish()
self._last_compute_monotonic = time.monotonic()
```
  keeping the existing expiry-refresh and error branches around it (read the current body and integrate; do not drop the error sleep).
- Tests (`unittest.IsolatedAsyncioTestCase` or `asyncio.run`): patch `_compute_and_publish` with a counting coroutine and `OPTIONS_CHAIN_MIN_INTERVAL_S=0.25`, cadence 5: start the cadence task, `mark_dirty()` 5 times within 0.1 s → exactly 1 compute within 0.5 s; with no dirty marks and `cadence_sec=0.3` → a compute still happens within ~0.4 s (timer ceiling); cancel the task at the end.

### Task 3: Docs + final run
- `documents/kite-algo-platform-reference.md` option-chain cadence lines: tick-driven (≥ `OPTIONS_CHAIN_MIN_INTERVAL_S`, default 1 s) with the 5 s cadence as the ceiling.
- Run `tests/options -q` → PASS. Final report per AGENTS.md.
