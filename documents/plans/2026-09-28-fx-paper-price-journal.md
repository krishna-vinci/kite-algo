# Paper Fill Price Fallback + Hosted Journal Diagnosis Plan

> **For agentic workers:** Execute task-by-task, in order. Do not load brainstorming or other workflow skills. AGENTS.md is not tracked in git: read `/home/krishna/kite-algo/AGENTS.md`. If a step does not fit the real code, stop and report the mismatch (file:line, what you saw) instead of improvising.

**Context (live paper test 2026-09-28, strategy `hs_c5f7be5d2fb94bfd8da709b9efd073e4`, account `kite:paper-a`):**
1. The first hosted paper MARKET order for NSE:ITC was rejected "No reference price available for paper execution": `PaperTradingService._place_order_locked` (`backend/paper_runtime/service.py:126-129`) prices from `_market_snapshot` (:935), which only reads `MarketDataRuntime.get_tick` → `latest_ticks` / `prime_tick_cache` (`backend/broker_api/orders/market_runtime_client.py:360-371`). ITC was not subscribed in market-runtime, so no tick existed. The retry passed only because a tick had arrived by then.
2. Hosted paper fills and decisions did not reach the journal: `strategy_proposal_journal` has only received/plan-created rows; no execution facts, no `JournalDecisionEvent`s, no source links for the runs; 10 paper journal runs stay `open`. Decision path: `backend/strategies/journal_bridge.py` (`record_decision`, `record_request_decision`; recorder installed in `backend/app/bootstrap.py`). Fill path claimed by its docstring: "paper via the paper runtime's attribution".

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-paper-journal`, branch `codex/fx-paper-journal`. Never touch `.env*`. Do NOT commit. No container restarts.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root. PG tests only on `127.0.0.1:15433`.
- Production DB is READ-ONLY for you: `docker exec kite-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT ..."'`. `docker logs kite-app` is allowed.

## Task 1: Paper fill price falls back to a fresh broker LTP

- Find the backend function the worker/SDK quotes route uses (SDK `ctx.client.get_quotes` → `/api/algo-workers/...quotes` → a service in `backend/api/services/market_data.py` or `backend/broker_api/broker_api.py:556` `kite.ltp`). That route returned ITC's price while the tick cache had none — reuse its broker LTP path.
- `PaperTradingService` gets an optional injected `ltp_fallback: Callable[[exchange, tradingsymbol], Awaitable[Optional[float]]]` (constructor arg, default `None` = today's behaviour). `_market_snapshot` (or `_place_order_locked`) calls it ONLY when there is no tick and no cached last price; bounded by `asyncio.wait_for(..., 2.0)`; any error → treated as no price (reject exactly as today).
- Record the source in the paper order's metadata: `price_source` = `"tick"` | `"broker_ltp"` | `"limit_price"` etc. (whatever branch `_reference_price` took).
- NEVER fill at the frozen plan `reference_price` (it can be minutes old after approval).
- Wire the fallback where the app builds the paper service (grep `PaperTradingService(` in `backend/app/`); strategy-runner or other processes keep `None` unless they already build it the same way — report which.
- Also: after a paper fill, the instrument must be subscribed for MTM. Check whether the paper engine's `sync_subscriptions` (paper_runtime/market_engine.py) subscribes tokens of open paper positions; report yes/no with file:line. Do not change it in this task.
- Tests (extend tests for `backend/paper_runtime/service.py`, find the file): no tick + fallback returns 266.45 → fill at 266.45 with `price_source="broker_ltp"`; no tick + fallback raises/times out → rejected as today; tick present → fallback not called.

## Task 2: Journal diagnosis ONLY (no fix)

Find the root cause of fact 2 with evidence. Do not change code for this task.
- Trace the paper fill → journal path: from `PaperTradingService` fill (attribution `source="hosted_plan_execution"`, `strategy_run_id`, `plan_id`) to the journal (find the projector/consumer: grep `journal` in `backend/paper_runtime/`, `backend/journaling/`, `backend/strategies/`). Is it called at all in the app process? What keys does it use to find/create the journal run and source link?
- Trace the decision path: `record_request_decision` → installed recorder (bootstrap) → how it finds the journal run ("same source keys the fill projectors use"). Why were zero decision events written? (e.g. no run found because no source link; environment filter; recorder never installed; exception swallowed — check `docker logs kite-app --since 2h 2>&1 | grep -i journal`).
- Why do 10 paper journal runs stay `open`, and which ones are they (ids, created_at, source)?
- Report: root cause(s) with file:line and the prod rows/log lines that prove it, and a concrete proposed fix (files, functions, what changes, which tests). Keep it tight.

## Final report per AGENTS.md (Task 1 changes + tests; Task 2 diagnosis).
