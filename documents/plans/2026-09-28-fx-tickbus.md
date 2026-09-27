# Shared In-Process Tick Bus Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. If a step does not fit the real code, stop at that step and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** Each process decodes each Redis tick once. finance-app's six `market:ticks` consumers read from `MarketDataRuntime` (which already decodes every tick) instead of opening their own Redis pub/sub; alerts-worker's per-instrument `RedisTickSource`s share one pub/sub connection per process.

**Prerequisite:** `codex/fx-protection` is merged into `development` — it adds `MarketDataRuntime.add_tick_listener(callback) -> unsubscribe` (`backend/broker_api/orders/market_runtime_client.py`). This branch is created from `development` after that merge. Verify: `grep -n "def add_tick_listener" backend/broker_api/orders/market_runtime_client.py` must match before you start; if not, stop and report.

**Architecture:** Add `MarketDataRuntime.subscribe_ticks(maxsize=10000) -> TickSubscription`: an `asyncio.Queue` fed by a tick listener; when full, the oldest tick is dropped and a per-subscription `dropped` counter increments (a slow consumer can never stall the feed). Each finance-app consumer takes an optional `tick_source` (the runtime) and, when given, iterates its subscription instead of opening a pub/sub; when not given (unit tests, other processes) it keeps today's Redis path unchanged. For alerts-worker, a process-local `SharedTickFanout` in `backend/workflows/runtime.py` owns one pub/sub and hands each `RedisTickSource` its own queue, preserving each source's per-instance epoch semantics.

**Tech Stack:** Python 3.11 asyncio, redis.asyncio, pytest.

**Spec:** Owner decision 2026-09-28; Explore facts: finance-app subscribers — `MarketDataRuntime._ticks_loop` (market_runtime_client.py:249), `CandleAggregator._market_runtime_tick_loop` (broker_api/market/candle_aggregator.py:233-283), `AlgoRuntimeLiveWorker` ticks loop (algo_runtime/live.py:89-91, handler :282-301), `PaperMarketEngine._run` (paper_runtime/market_engine.py:80-107), `MarketStreamHub._ticks_loop` (alerts/market_stream.py:474-510), SSE quote stream (api/services/market_data.py:1093-1150, one pub/sub per HTTP request); alerts-worker: `RedisTickSource` (workflows/runtime.py:218-~370), created per instrument by `tick_source_factory` (workflows/worker_entry.py:812-823). Bootstrap wiring: backend/app/bootstrap.py (market data runtime :461-463, candle aggregator :659-677/:694, algo live :741-747, paper engine :748-754).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-tickbus`, branch `codex/fx-tickbus`. Read `AGENTS.md` first.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root. Never touch `.env*`. Do NOT commit.
- Behaviour must not change for any consumer except its tick source: same per-tick handling, same filters, same heartbeats. Each consumer's existing tests must pass unmodified (they use the Redis path).
- Tick payload shape: consumers today `json.loads` the pub/sub payload into a dict. The bus delivers `MarketDataRuntime`'s normalized tick dict — read `_normalize_tick_payload` and, for each consumer, confirm every field it reads exists in the normalized dict with the same meaning; if a consumer needs a raw field the normalizer drops, deliver the raw decoded payload instead (add it to the listener callback as `tick["_raw"]` ONLY if needed, and report it).

## Decisions (fixed)

1. Queue: per subscription `asyncio.Queue(maxsize)`; default 10000; on `QueueFull` → `get_nowait()` one then `put_nowait`, `dropped += 1`, `logger.warning` at most once per 60 s per subscription with the count.
2. The SSE quote stream opens a subscription per request and MUST `close()` it in a `finally` (unsubscribe listener) — no leaked listeners.
3. Consumers switch to the bus only when constructed with `tick_source=<MarketDataRuntime>`; bootstrap passes `app.state.market_data_runtime`.
4. alerts-worker fan-out: one module-level `SharedTickFanout` per process, lazily started on first `RedisTickSource.start()`, stopped when the last source closes; each source registers a new queue on `start()` (fresh epoch preserved) and unregisters on close. Pull model (`next_observation()` with `timeout=1.0`) stays: it reads its queue with `asyncio.wait_for(queue.get(), 1.0)` → `None` on timeout.

## Tasks

### Task 1: `subscribe_ticks` on `MarketDataRuntime`
- Produces `class TickSubscription` (in market_runtime_client.py): `async get() -> dict`, `get_nowait() -> dict | None`, `close()`, `dropped: int`, async iterator support (`async for tick in sub`). `MarketDataRuntime.subscribe_ticks(maxsize: int = 10000) -> TickSubscription` uses `add_tick_listener`.
- Tests (tests/broker_api/test_market_runtime_client.py): ticks delivered in order; a full queue drops oldest and counts; `close()` stops delivery; a closed subscription's listener is removed (listener count back to previous).

### Task 2: Migrate finance-app consumers (one sub-task each, test after each)
For each of: CandleAggregator, AlgoRuntimeLiveWorker (ticks loop only — its candles/orders/positions loops stay), PaperMarketEngine (keep its 5 s `sync_subscriptions()` cadence: run it on a timer task or check elapsed time inside the consume loop), MarketStreamHub, SSE quote stream:
- Add optional `tick_source` constructor/factory parameter (for MarketStreamHub the lazy singleton `get_market_stream_hub()` reads `app.state.market_data_runtime` via its caller — pass it from `alerts_market_ws.py` if the hub is created there; for the SSE stream use the service's app/runtime reference already available in `market_data.py`, else pass it in from the router).
- When set: consume `subscription = tick_source.subscribe_ticks()` and feed each tick into the SAME per-tick handler the Redis path calls (same filtering), then `subscription.close()` on shutdown.
- New test per consumer: construct it with a fake `tick_source` whose `subscribe_ticks()` returns a fake subscription yielding two ticks; assert the per-tick handler received both and no Redis pubsub was created (patch `get_redis`/the redis factory to raise if called).
- Wire in `backend/app/bootstrap.py`: pass `app.state.market_data_runtime` to each (it is created at :461-463 before the others — verify ordering).
- Run each consumer's existing test file after its change → PASS.

### Task 3: alerts-worker `SharedTickFanout`
- In `backend/workflows/runtime.py` add `SharedTickFanout` (one pub/sub on `MARKET_TICKS_CHANNEL`, a reader task decoding each message once and putting the dict into every registered queue with the drop-oldest rule) and make `RedisTickSource.start()/close()` register/unregister; `next_observation()` reads its queue (same freshness/lag logic after decoding).
- Tests in `tests/workflows/test_ltp_freshness.py` (or a new `tests/workflows/test_shared_tick_fanout.py`): two sources share ONE pubsub subscribe call (fake redis counts `subscribe`), each gets every tick, closing both stops the reader; a new source after an outage gets a fresh epoch (reuse the existing epoch assertion pattern).

### Task 4: Measure + docs + final run
- Add a debug counter: `MarketDataRuntime` exposes `tick_listener_count` and total `dropped` across subscriptions in its existing status/heartbeat meta (find where it reports status; add two fields).
- Docs: `documents/kite-algo-platform-reference.md` — market data section: one decode per process; consumers via `subscribe_ticks`; alerts-worker shared fan-out.
- Final run: all touched test files, plus `tests/broker_api -q`, `tests/algo_runtime -q`, `tests/workflows -q`, `tests/api/test_alerts_market_ws.py -q`. Report pre-existing failures separately (also failing on `development`).
- Final report per AGENTS.md, including the tick-field compatibility check result per consumer.
