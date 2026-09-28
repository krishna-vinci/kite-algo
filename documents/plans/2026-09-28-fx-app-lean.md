# Lean kite-app Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Steps use checkbox (`- [ ]`) syntax. Do not load any skill. AGENTS.md is not tracked: read `/home/krishna/kite-algo/AGENTS.md`. If a step does not fit the real code, stop at that step and report the mismatch (file:line, what you saw) instead of improvising.

**Goal:** Cut finance-app (`kite-app`) RSS from ~430 MB and its remaining tick-path CPU without changing any behaviour: lazy-load libraries only rare paths need, faster JSON on the hot path, uvloop/httptools, a glibc arena cap, no per-minute DB connection in the chain reaper, and `gc.freeze()` after startup.

**Architecture:** Packaging + import-placement + runtime-knob changes only. No calculation, protection, chain, order or API behaviour changes. Every change is verified by the existing targeted suites plus a before/after RSS + import-time measurement.

**Tech Stack:** Python 3.14 image (`python:3.14-slim`), FastAPI/uvicorn, numba, orjson, uvloop, httptools.

**Spec:** Owner request 2026-09-28 ("make kite-app ultra efficient without hurting anything"). Evidence gathered in the running container:
- RSS 431 MB (anon 299 MB), 24 threads; `MALLOC_ARENA_MAX` unset.
- Heavy libs mapped: scipy (430 mappings), pandas (215), numpy, numba/llvmlite.
- First importers (import hook trace): `mibian` ← `backend/broker_api/options/options_greeks.py:21` (pulls `scipy.stats`, 0.93 s import); `numba` ← `options_greeks.py:9` (numba itself also imports base scipy); `pandas` ← `backend/api/routers/fundamentals.py:30` (then `backend/api/services/indicators_service.py:14`, `backend/broker_api/performance/performance_logic.py:3`).
- mibian is used only by the legacy calculator method `calculate_greeks` in `options_greeks.py` (~:396-416).
- py-spy (30 s, market hours): stdlib `json` `raw_decode` is the top leaf on the tick path (`backend/broker_api/orders/market_runtime_client.py:353` and `:430`); snapshot encode `backend/options/market/redis_cache.py:44`, decode `:53`.
- uvloop, httptools, orjson are NOT installed. `backend/requirements.txt` lists `uvicorn` (unpinned), `pandas`, `mibian`, `numba`; Dockerfile does `pip install --no-cache-dir -r requirements.txt`.
- The chain reaper calls `backend.strategies.market_session.session_state("NFO")` every 60 s, which opens a fresh psycopg2 connection via `_default_trading_day_reader` each time.

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-app-lean`, branch `codex/fx-app-lean`. Never touch `.env*`. Do NOT commit. "Checkpoint" = `git status`.
- Do not remove numba or change any numeric code. Do not change public APIs, routes, response shapes, or DB schema.
- Python tests: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root.
- Measuring: build a throwaway image from the worktree (`docker build -t kite-lean-check -f Dockerfile .`) and measure inside it with `docker run --rm --env-file <none> ...` ONLY for import-time/RSS of `python -c "import backend.main"` (no DB needed for import; if import needs env, pass dummy values inline with `-e`, never read `.env`). Do NOT restart or touch the running `kite-app`.

## Decisions (fixed)

1. `mibian` is imported lazily inside the legacy `calculate_greeks` method only; module import of `options_greeks` must not import mibian/scipy.stats. If mibian is missing at call time, keep today's behaviour for that method (read the current `except ImportError` fallback and preserve it).
2. pandas is imported lazily (inside functions) in every module that is imported at app startup; target: `import backend.main` must not import pandas. Find ALL startup importers with the hook in Task 2 Step 1 (fundamentals router first; there may be more after it). Type hints that reference `pd.DataFrame` use `from __future__ import annotations` + `if TYPE_CHECKING: import pandas as pd`.
3. JSON: add `orjson` and use it ONLY on hot paths: tick decode in `market_runtime_client.py` (both `json.loads` sites) and option snapshot encode/decode in `redis_cache.py`. Wrap in a helper `backend/shared/fastjson.py` with `loads(bytes|str)` and `dumps_str(obj, *, default=str) -> str` (orjson returns bytes → decode to str; handle `default=str` via orjson's `default=` callable; `OPT_NON_STR_KEYS` for int keys). Fallback to stdlib `json` if orjson import fails. Output of `dumps_str` must round-trip through stdlib `json.loads` identically for the snapshot payload (test it). NaN handling: stdlib `json.dumps` emits `NaN`, orjson emits `null` — find whether snapshots can contain NaN (numpy floats from Greeks); if they can, convert NaN → None before encoding in the helper and state in the report that readers get `null` (check `read_option_snapshot_from_redis` and the frontend Options page tolerate null — grep).
4. uvicorn: replace `uvicorn` with `uvicorn[standard]` in `backend/requirements.txt` (brings uvloop + httptools; uvicorn auto-selects them). Keep the compose command unchanged.
5. glibc arenas: add `MALLOC_ARENA_MAX: "2"` to the `environment:` of `finance-app` in `compose.yml`, `alerts-worker` in `compose.worker.yml`, and `strategy-runner` in `compose.supervisor.yml`.
6. Reaper: cache the NFO trading-day answer per IST date inside `OptionsSessionManager` (one `session_state` call per minute is fine, but the calendar read must be cached per date): add a small per-date cache around the `trading_day_reader` passed to `session_state` (use the `trading_day_reader=` parameter `session_state` already accepts). Market-hours/after-close logic unchanged.
7. `gc.freeze()` once at the end of startup in `combined_lifespan` (after all background tasks start, before `yield`), plus `gc.collect()` right before it. Nothing else about GC.

## File Structure

- Modify `backend/broker_api/options/options_greeks.py` (mibian lazy).
- Modify startup importers of pandas (at least `backend/api/routers/fundamentals.py`, `backend/api/services/indicators_service.py`, `backend/broker_api/performance/performance_logic.py`; others found by the hook).
- Create `backend/shared/fastjson.py` (check `backend/shared/` exists; if not, place it at `backend/broker_api/core/fastjson.py` and report).
- Modify `backend/broker_api/orders/market_runtime_client.py:353,:430`, `backend/options/market/redis_cache.py:44,:53`.
- Modify `backend/requirements.txt` (`uvicorn[standard]`, `orjson`).
- Modify `compose.yml`, `compose.worker.yml`, `compose.supervisor.yml` (env var only).
- Modify `backend/broker_api/options/options_sessions.py` (reaper calendar cache).
- Modify `backend/app/bootstrap.py` (`gc.freeze`).
- Tests: `tests/shared/test_fastjson.py` (or next to the helper's package), `tests/options/test_options_redis_cache.py`, `tests/broker_api/test_market_runtime_client.py`, `tests/options/test_options_sessions.py`, and a new `tests/app/test_startup_imports.py`.

---

### Task 1: Baseline measurement

- [ ] **Step 1:** Build the image from the UNCHANGED worktree: `docker build -q -t kite-lean-before -f Dockerfile .`
- [ ] **Step 2:** Measure import RSS/time (dummy env, no DB):
```bash
docker run --rm -e DB_HOST=x -e DB_PASSWORD=x -e REDIS_URL=redis://x:6379/0 kite-lean-before python -c "
import time,resource;t=time.time();import backend.main;import sys
print('import_s=%.2f rss_mb=%d modules=%d pandas=%s scipy_stats=%s mibian=%s' % (time.time()-t, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024, len(sys.modules), 'pandas' in sys.modules, 'scipy.stats' in sys.modules, 'mibian' in sys.modules))"
```
  If `import backend.main` fails without a real DB/env, report the error and measure `import backend.app.bootstrap, backend.api.routers` instead (same command). Record the numbers.

### Task 2: Lazy mibian + lazy pandas

- [ ] **Step 1:** Write `tests/app/test_startup_imports.py`:
```python
"""Heavy libraries only rare paths need must not load at app import."""
import subprocess
import sys


def test_app_import_does_not_load_pandas_mibian_or_scipy_stats():
    code = (
        "import sys; import backend.app.bootstrap, backend.api.routers; "
        "print(','.join(m for m in ('pandas','mibian','scipy.stats') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "", out.stdout
```
  (Use the same module list Task 1 managed to import.) Run → FAIL (lists `pandas,mibian,scipy.stats`).
- [ ] **Step 2:** In `options_greeks.py` remove the module-level `try: import mibian ... except ImportError: mibian = None`; inside the legacy `calculate_greeks` method add the same try/except locally so behaviour when mibian is absent is identical.
- [ ] **Step 3:** Find every startup importer of pandas: run
```bash
/home/krishna/kite-algo/.venv/bin/python - <<'PY'
import builtins, traceback, sys
orig = builtins.__import__
def hook(name, *a, **k):
    if name.split('.')[0] == 'pandas' and 'pandas' not in sys.modules:
        print(' <- '.join(f"{f.filename.split('backend/')[-1]}:{f.lineno}" for f in reversed(traceback.extract_stack()[:-1]) if '/backend/' in f.filename))
    return orig(name, *a, **k)
builtins.__import__ = hook
import backend.app.bootstrap, backend.api.routers
PY
```
  Fix the first importer (move `import pandas as pd` into the functions that use it; `TYPE_CHECKING` for annotations), re-run, repeat until nothing prints. Keep a list.
- [ ] **Step 4:** Run the startup-import test → PASS. Run the test files of every module you touched (find with `grep -rl "<module name>" tests | head`) → PASS.
- [ ] **Step 5:** Checkpoint.

### Task 3: orjson on the hot paths

- [ ] **Step 1:** Add `orjson` and change `uvicorn` → `uvicorn[standard]` in `backend/requirements.txt`. Install into the host venv for tests: `/home/krishna/kite-algo/.venv/bin/pip install -q orjson "uvicorn[standard]"`.
- [ ] **Step 2:** Tests `tests/shared/test_fastjson.py` (create the package dir with `__init__.py` only if sibling test packages have one): `loads` accepts str and bytes; `dumps_str` returns `str`; a snapshot-shaped dict with a `date`, `datetime`, int dict keys and a NaN float round-trips via stdlib `json.loads` (NaN → null per Decision 3); fallback path works when orjson import is patched to fail (use `importlib.reload` with `sys.modules['orjson']=None`).
- [ ] **Step 3:** Implement `fastjson.py` per Decision 3; replace the two `json.loads` in `market_runtime_client.py` and `json.dumps`/`json.loads` in `redis_cache.py` with the helper.
- [ ] **Step 4:** Run `tests/shared/test_fastjson.py tests/options/test_options_redis_cache.py tests/broker_api/test_market_runtime_client.py tests/options -q` → PASS.
- [ ] **Step 5:** Checkpoint.

### Task 4: Reaper calendar cache + gc.freeze + arena cap

- [ ] **Step 1:** Test in `tests/options/test_options_sessions.py`: calling the manager's reap step 3 times on the same IST date invokes the trading-day reader once (inject a counting reader via the new cache's seam). Implement per Decision 6. Run `tests/options/test_options_sessions.py -q` → PASS.
- [ ] **Step 2:** Add `gc.collect(); gc.freeze()` at the end of startup in `combined_lifespan` (Decision 7), with a one-line comment. Verify import: `/home/krishna/kite-algo/.venv/bin/python -c "import backend.app.bootstrap"`.
- [ ] **Step 3:** Add `MALLOC_ARENA_MAX: "2"` per Decision 5. Validate: `docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml config --services` succeeds (print only `--services`).
- [ ] **Step 4:** Checkpoint.

### Task 5: After measurement + docs + final report

- [ ] **Step 1:** `docker build -q -t kite-lean-after -f Dockerfile .` and re-run the Task 1 Step 2 command against `kite-lean-after` with `-e MALLOC_ARENA_MAX=2`. Also confirm uvloop is picked: `docker run --rm kite-lean-after python -c "import uvloop, httptools, orjson; print('ok')"`. Record before/after.
- [ ] **Step 2:** Remove both throwaway images: `docker rmi kite-lean-before kite-lean-after`.
- [ ] **Step 3:** Docs: add a short "Runtime efficiency" note to `documents/kite-algo-platform-reference.md` (lazy heavy imports, orjson hot paths, uvloop, MALLOC_ARENA_MAX, gc.freeze).
- [ ] **Step 4:** Final targeted run: `tests/app/test_startup_imports.py tests/shared -q`, `tests/options -q`, `tests/broker_api -q`, `tests/api/test_platform_routes.py -q`, plus test files for every pandas-lazy module. Report pre-existing failures separately (confirm on `development`).
- [ ] **Step 5:** AGENTS.md final report with the before/after table (import time, RSS, modules, pandas/scipy.stats/mibian loaded), the list of pandas-lazy modules, and the NaN/null decision.

## Additional tasks (from the GLM efficiency audit, RAM/CPU only)

### Task 6: Candle aggregator idles with no tokens
- Facts: `backend/broker_api/market/candle_aggregator.py:127` starts four loops (30 s refresh, 60 s persistence, tick loop, 25 s lease) even when desired tokens are empty (log "Runtime candle subscriptions synced: 0 tokens" ~every 13 s).
- Change: when the desired token set is empty, the persistence and lease loops skip their work (no HTTP/DB) and the refresh loop keeps only its cheap desired-token recomputation; nothing is subscribed. Work resumes on the first refresh that finds tokens. Do not change behaviour when tokens exist.
- Test in `tests/broker_api/test_candle_aggregator.py`: with zero tokens, N iterations of persistence/lease do no repository/HTTP calls (fakes count calls); with tokens they do as before.

### Task 7: Order worker off-hours cadence
- Facts: order worker loop every 1 s (`backend/app/bootstrap.py:511`) and broker positions/trades reconcile every 30 s (`bootstrap.py:537`, `backend/broker_api/orders/order_runtime.py:935`), 24x7.
- Change: outside the NSE/NFO/MCX sessions (use `backend.strategies.market_session.session_state` for NSE and MCX; "in session" = either open, or within 30 min before open / 30 min after close), the 1 s order-worker poll becomes 15 s and the 30 s broker reconcile becomes 600 s. Inside those windows nothing changes. Order/fill WebSocket events keep being processed immediately regardless (do not touch the event path). Make both intervals env-overridable (`ORDER_WORKER_OFFHOURS_INTERVAL_S`, `POSITIONS_RECONCILE_OFFHOURS_INTERVAL_S`). Calendar reads must be cached per IST date (reuse the Task 4 cache helper if generic, else a local per-date cache).
- Tests: a pure function `order_loop_interval(now, *, session_open_fn) -> (poll_s, reconcile_s)` covering in-session, pre-open window, after-close window, weekend.

### Task 8: Log volume
- Facts: one INFO per token/interval upsert at `backend/candle_storage.py:91` (verify path) and one INFO per finalized candle at `backend/broker_api/market/candle_aggregator.py:441`; the "subscriptions synced" INFO repeats every sync.
- Change: those per-item logs → DEBUG; add one INFO summary per minute (counts) in the aggregator; log "subscriptions synced" at INFO only when the token count changes. No test needed beyond existing files passing.

Final run additions: `tests/broker_api/test_candle_aggregator.py tests/broker_api/test_order_runtime.py -q` and the new interval tests.
