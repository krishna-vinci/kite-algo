# Hybrid Option Chain (efficient + on-demand + owner settings) Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. AGENTS.md is not tracked in git: read `/home/krishna/kite-algo/AGENTS.md`. If a step does not fit the real code, stop and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** Cut the option-chain CPU from ~50–150 % of `kite-app` to a few percent while keeping server-side chains for protection and delta selection: vectorized Greeks, cached max pain and forward instruments, cheaper publishing, only owner-chosen underlyings always on (default NIFTY), others started on demand and stopped when idle, and all of it configurable from the app (DB-backed, applied live).

**Architecture:** Compute changes stay in `backend/broker_api/options/options_sessions.py`, `options_greeks.py`, `backend/options/market/analytics/max_pain.py`, `backend/options/market/redis_cache.py`. Session lifecycle: `OptionsSessionManager` records `last_used` per underlying on every read (`get_snapshot`/service reads, protection Greeks loader, API) and a reaper stops non-always-on sessions idle longer than `idle_stop_minutes`; a read for a non-running underlying schedules `ensure_session` (non-blocking) and the caller sees "unavailable" until the first snapshot (existing refusal `OPTION_CHAIN_SNAPSHOT_UNAVAILABLE`). Settings live in a new single-row table `platform_options_settings` (migration `20260928_000057`), exposed by `GET/PUT /api/platform/options-settings`, applied live through `update_config` and the manager — no restart.

**Measured baseline (in-container microbenchmarks, 2026-09-28):** max pain 61 strikes 4.21 ms/expiry (pure Python O(n²)); per-contract Greeks 1.16 ms/expiry vs vectorized 0.018 ms; snapshot 189 KB json.dumps 3.8 ms ×2 publishes; forward lookup = a Postgres query per expiry per compute. py-spy: `_run_computation` 42 % of samples, max pain 15 %, publish/serialize ~14 %, `_compute_forward` 7 %.

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-chain-hybrid`, branch `codex/fx-chain-hybrid`. Do NOT touch `frontend-next/` (a separate agent builds the UI against the contract below). Never touch `.env*`. Do NOT commit.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root. PG only `127.0.0.1:15433`.
- Numbers must not change: every existing option test must still pass (Greeks/IV/forward/selection values identical within 1e-9 except where a task says otherwise).
- Backwards compatible: env `OPTIONS_AUTOSTART_UNDERLYINGS` still works when no settings row exists.

## API contract (shared with the frontend agent — implement exactly)

`GET /api/platform/options-settings` (owner auth, same dependencies as `/api/platform/live-settings` in `backend/api/routers/platform.py:122`) →
```json
{
  "always_on": ["NIFTY"],
  "available_underlyings": ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "BANKEX"],
  "cadence_sec": 5,
  "tick_driven": true,
  "min_interval_sec": 1.0,
  "idle_stop_minutes": 15,
  "source": "db" | "default",
  "updated_at": "…" | null,
  "updated_by": "…" | null,
  "sessions": [
    {"underlying": "NIFTY", "running": true, "always_on": true, "last_used_age_s": 3.2,
     "updated_age_s": 0.8, "desired_tokens": 250, "cadence_sec": 5}
  ]
}
```
`PUT /api/platform/options-settings` (same-origin enforced like live-settings PUT) body:
```json
{"always_on": ["NIFTY"], "cadence_sec": 5, "tick_driven": true, "min_interval_sec": 1.0, "idle_stop_minutes": 15, "reason": "…"}
```
Validation (422 on violation): `always_on` ⊆ `available_underlyings`, ≤ 3 entries; `cadence_sec` int 1–10; `min_interval_sec` 0.25–10 and ≤ `cadence_sec`; `idle_stop_minutes` int 0–390 (0 = never stop until market close — reaper skips). Response = same shape as GET. Every PUT appends an audit row (who, when, old→new, reason) like live-settings.

## Tasks

### Task 1: Vectorized Greeks per expiry
- Add to `options_greeks.py` a numba kernel `black76_greeks_arrays(is_call: np.ndarray[bool], F: float, K: np.ndarray, T: float, sigma: np.ndarray) -> (delta, gamma, theta, vega)` (njit, loops over arrays, NaN sigma → NaN outputs), reusing the existing scalar maths so values are identical.
- In the vectorized path of `_run_computation`, build arrays for all (strike, CE/PE) contracts of the expiry that have a usable sigma and compute Greeks in ONE call; keep the same post-processing (theta/365, vega/100, rho=0/None as today) and the same row dict fields.
- Test: for a synthetic chain (reuse `_make_chain` in tests/options/test_options_sessions.py), the row Greeks from the new path equal the old per-contract results within 1e-9 for every contract; a contract with unsolved IV still gets the same output as today.

### Task 2: Max pain vectorized + cached
- `compute_bounded_max_pain` → numpy implementation (arrays of strikes, CE OI, PE OI; pain matrix via broadcasting) with identical result and tie-break (lowest strike among minimal pain). Keep the function signature.
- In sessions, recompute max pain (and PCR, if it is similarly expensive — measure) per expiry at most every `OPTIONS_MAX_PAIN_REFRESH_S` (default 30 s); reuse the cached value in between.
- Tests: numpy result == old implementation result on 5 randomized OI sets (keep a copy of the old function in the test as the oracle); cache returns the same value within 30 s and recomputes after.

### Task 3: Forward instruments cached
- `_compute_forward` must not query the DB each compute: fetch the candidate strikes' instruments through the existing `_get_cached_instruments` cache (same cache TTL semantics as the window lookup) keyed by `(underlying, expiry, strikes tuple)`.
- Test: two computes with the same strikes call the repo's `get_option_instruments_for_strikes` once (count via fake repo).

### Task 4: Cheaper publishing
- Find every reader of the legacy pub/sub channel `options:updates:<U>` (`publish_event(pub_channel, session.snapshot)` in `on_session_update`): grep backend/, frontend-next/, mcp/, sdk/, market-runtime/. If there is no reader, remove the publish. If there is a reader, publish it at most every 5 s. Report which.
- Serialize the v1 payload with `orjson` if it is installed in the image (`python -c "import orjson"` inside `kite-app`: `docker compose exec -T finance-app python -c "import orjson"`); if not installed, add `orjson` to `backend/requirements.txt` only if the build already pins compatible versions — otherwise keep `json` and report. Output must remain valid JSON readable by `read_option_snapshot_from_redis` (existing test).
- Skip the Redis SET+PUBLISH when the snapshot is unchanged except `updated_at`/health ages (compare a cheap digest of per_expiry rows' ltp/iv/oi and forward) — but still SET at least every 5 s so the key's freshness and TTL hold.

### Task 5: Settings table, API, live apply
- Migration `backend/alembic/versions/20260928_000057_platform_options_settings.py` (`down_revision = "20260927_000056"`): table `platform_options_settings` (single row: `settings_id` PK fixed 1, `always_on` JSON, `cadence_sec` INT, `tick_driven` BOOL, `min_interval_sec` NUMERIC, `idle_stop_minutes` INT, `updated_by`, `updated_at`) and `platform_options_settings_audit` (append-only). Mirror in `backend/schema.sql` and add SQLAlchemy models next to the live-settings models in `backend/platform/models.py`. Follow exactly how `platform_live_settings` is done (read `backend/platform/settings.py` and its migration `20260926_000052`).
- `backend/platform/options_settings.py`: `read_options_settings()` (row or defaults: always_on from env `OPTIONS_AUTOSTART_UNDERLYINGS` or `["NIFTY"]`, cadence 5, tick_driven true, min_interval 1.0, idle 15) and `update_options_settings(...)` with validation + audit.
- Routes in `backend/api/routers/platform.py` per the contract; `sessions` comes from the manager (`app.state.options_session_manager`), `[]` when absent.
- Live apply: after a successful PUT, call a manager method `apply_settings(settings)` that (a) ensures always-on sessions are started, (b) calls each running session's `update_config(window_size, cadence_sec)` with the new cadence (keep its window), (c) sets tick-driven on/off and min interval on sessions (replace the env read of `OPTIONS_CHAIN_MIN_INTERVAL_S` with the session attribute, env as fallback default), (d) marks sessions no longer always-on as on-demand (they will be reaped when idle). Bootstrap `autostart_option_sessions` uses `read_options_settings().always_on` instead of `autostart_underlyings()` (keep env fallback via the defaults).
- Tests: `tests/api/test_platform_routes.py` — GET defaults, PUT round-trip + audit row + 422 cases; manager `apply_settings` unit test with a fake session recording `update_config` calls.

### Task 6: On-demand sessions, keep-alive, idle reaper
- Manager: `touch(underlying)` records `time.monotonic()`; call it from `get_snapshot` (or the single read entry point the service uses — find it: `OptionsMarketService` reads `manager.get_snapshot`), from the protection Greeks loader path (it goes through `OptionsMarketService.get_greeks`, so touching in the service read covers it), and from `market_router` reads.
- If a read targets an underlying without a running session: schedule `asyncio.create_task(self.ensure_session(u))` once (guard against duplicate scheduling) and return what it returns today for "no session".
- Reaper task started with the manager (every 60 s): for each running session not in `always_on` with `idle_stop_minutes > 0` and `now - last_used > idle_stop_minutes*60` → `stop_session(u)`. After market close (IST ≥ 15:35 on NSE days, use `backend/strategies/market_session.session_state("NFO")`), on-demand sessions stop regardless.
- Tests: touch/ensure scheduling (a read for a stopped underlying schedules exactly one ensure), reaper stops an idle on-demand session and never an always-on one, `idle_stop_minutes=0` never reaps during market hours.

### Task 7: Measure + docs + final run
- Re-run the microbenchmarks from "Measured baseline" inside the worktree (host venv) with the new code and report before/after per piece.
- Docs: `documents/kite-algo-platform-reference.md` option-chain section (hybrid lifecycle, settings API, cadence defaults, vectorized Greeks, cached max pain).
- Final run: `tests/options -q`, `tests/api/test_platform_routes.py -q`, `tests/broker_api/test_market_runtime_client.py -q`, `tests/api/test_protection_scheduler.py tests/sdk/test_worker_protection_runtime.py -q`, and the migration via `ADMISSION_PG_URL=postgresql://postgres:testonly@127.0.0.1:15433/kite_test RECONCILIATION_PG_ADMIN=postgresql://postgres:testonly@127.0.0.1:15433/postgres /home/krishna/kite-algo/.venv/bin/python -m pytest tests/integration/test_options_structures_postgres.py -q` (it upgrades to head). Report pre-existing failures separately.
- Final report per AGENTS.md.
