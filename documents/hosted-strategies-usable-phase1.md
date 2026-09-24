# Hosted strategies - Phase 1 (data and dependency foundation) report

Date: 2026-09-23. Status: **ready for review** (Astra owns acceptance; nothing
here is self-accepted). Task `/root/hosted_data_foundation`, workspace
`/home/krishna/kite-algo`, baseline `27c58b4` (verified with
`git rev-parse HEAD`), prior production code `f0c747c`.

Implements **Phase 1 only** of
`documents/hosted-strategies-usable-platform-plan-2026-09-23.md`. Phases 2-5 are
untouched. No commit, stage, push, deploy, production migration, real broker
order, notification or session change was performed. The only PostgreSQL used is
the disposable instance on **15433**; production 15432 was never contacted.

## 1. What changed

Capability composition (`backend/strategies/service.py`)

- `data=true` now grants `market:read`, `market:stream`, `universes:read` and
  `universes:resolve` (`CHILD_MARKET_ACTIONS`, `CHILD_UNIVERSE_ACTIONS`).
- `data=false` grants none of them; its action set is exactly
  `{runs:read, runs:log, runs:progress}`.
- `trade=true` adds the paper order actions **and** `funds:read`
  (`CHILD_FUNDS_ACTIONS`); `notify=true` still adds only
  `notifications:publish`.
- `heartbeat` remains forbidden for every child (`validate_child_token_actions`
  refuses it), and `workflows:read`/`workflows:write` are still outside the child
  vocabulary - the new universe lens is membership *consumption* only.

Hosted read authority (`backend/api/services/hosted_attempt.py`,
`backend/api/routers/worker_shared.py`)

- New `enforce_hosted_read_authority(request, token)` and
  `hosted_owner_for_token(request, token)`. The decision is made from the
  persisted ledger only: the token must be the credential minted for the
  `strategy_jobs` row (`get_job_by_token_id`), `desired_state` must be
  `started`, the attempt status must be `starting`/`running`, and the lease must
  not have expired. Fenced/stopped/failed/hung/expired and
  hosted-shaped-but-unknown tokens are refused.
- `_validate_authority` now takes an optional run, so run-scoped mutation
  behaviour is unchanged and run-less reads share the same checks instead of a
  second implementation.
- `require_worker_read_action(request, token, action)` is the single read guard:
  action membership plus hosted attempt authority. External tokens take the
  cheap `token_is_hosted_candidate` pre-filter and are otherwise untouched, so
  the external contract is byte-identical.

Route coverage

- `backend/api/routers/worker_market.py`: every `market:read`/`market:stream`/
  `funds:read` read (instruments, quotes, candles, history, snapshot, streams,
  index constituents/status, calendar, funds, portfolio, indicators) uses the
  new guard.
- `backend/api/routers/fundamentals.py`: `_authorize` uses the guard.
- `backend/options/api/worker_options_router.py`: the token-only reads
  (session, expiries, chain, mini-chain, greeks, selection resolve, PCR,
  max-pain, strategy preview, run state, protection state) now require, for a
  hosted child only, **both** a live persisted attempt and the `data` lens
  (`market:read`). External callers are unchanged: no action is required of them
  and no attempt is looked up. Options **mutations** keep their existing
  `intents:submit`/`risk:update` + paper-only gates: no hosted live options
  mutation was unlocked.
- Streaming reads are revalidated while they run, not only at connect:
  `backend/api/services/market_data.py` wraps the tick and candle stream loops
  with `_HostedStreamAuthority`, which re-runs the same persisted-attempt check
  on a bounded interval
  (`HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS`, default 15 s, floor 0.25 s) and
  ends the stream with a `stream_closed` event naming the reason
  (`HOSTED_ATTEMPT_FENCED`, `HOSTED_ATTEMPT_STOPPED`, `HOSTED_LEASE_EXPIRED`, or
  the token-level refusal). External tokens never enter the check. The WebSocket
  market routes (`worker_ticks_ws`, `worker_candles_ws`) now also run the hosted
  attempt check before accepting and share the same revalidating generators.
  Each check revalidates **both** the persisted credential
  (`get_token_status(token_id)` plus the token's own expiry) and the persisted
  attempt, so a token-only revocation ends the stream too; the hosted candle
  path also bounds an *idle* reader (see 3b).
- `backend/api/routers/worker_universes.py`: reads accept hosted
  `universes:read`; resolve accepts hosted `universes:resolve`; the external path
  still requires `workflows:read`/`workflows:write` exactly as before. The
  hosted owner is derived from the persisted strategy (`strategy_jobs.owner_id`,
  an `app:<username>` identity) - never from the token's `account_scope`, which
  is a trading account, not application ownership. Hosted `create` is refused
  with `HOSTED_UNIVERSE_DEFINITION_FORBIDDEN`.

Session nonce: hosted **reads** are not session-nonce gated. The nonce
(`X-Worker-Session-Nonce`) governs run-session claim/heartbeat on run-bound
routes (`require_active_worker_run_session`), and the SDK already sends it
consistently via `session_headers(...)` on those calls. Since no read route
requires it, no SDK client change was needed; adding a nonce requirement to
reads would be a contract change, not a fix.

Dependencies and first-run readiness

- `Dockerfile.supervisor` installs the SDK with its `indicators` extra
  (`dataframe` + `indicators`: pandas, numpy, numba) at build time on the same
  `python:3.14-slim` base the platform image already uses. No runtime `pip`
  install path exists.
- `backend/strategies/supervisor.py`: the child environment allowlist now
  includes explicit numerical-runtime bounds
  (`OMP/OPENBLAS/MKL/NUMEXPR/VECLIB/NUMBA_NUM_THREADS=1`,
  `NUMBA_CACHE_DIR=<scratch>/.numba-cache`) and `_prepare_scratch` creates that
  cache directory child-owned. Containment was **not** relaxed: `RLIMIT_AS`
  remains 2 GiB and the uid/gid drop is unchanged.
- `backend/strategies/readiness.py`: reusable, pure `assess_source_readiness`
  (`ast` parse only - never import, compile or exec). It names syntax errors,
  a missing/nested/async/incompatible `main(ctx)` and statically visible imports
  the profile does not provide; dynamic imports and guarded optional imports are
  reported `unknown`, never as a pass. `profile_payload()` exposes the single
  documented profile `hosted-python-dataframe-indicators`.
  Required keyword-only parameters (`def main(ctx, *, required)`) are refused
  because the bootstrap calls `main(ctx)`, and an obvious module-level
  redefinition/reassignment/re-import/deletion of `main` is reported `unknown`
  on the entrypoint check with the overall result held at `blocked` - never a
  false "ready".
- Operator contract: `POST /api/strategies/readiness` (cookie auth + same-origin
  assertion, writes nothing) returns `SourceReadinessResponse`;
  `GET /api/strategies/options` now also returns `runner_profile` (existing
  fields unchanged).
- SDK: new `kite_algo_worker/readiness.py` typed mirror
  (`SourceReadiness`, `RunnerProfile`, `SourceImports`, ...), lazily exported
  from `kite_algo_worker`; `sdk/python/README.md` documents the hosted runner
  profile.

Index ticker

Index ticker prices/candles use the existing catalog and quote/candle routes (no
new endpoint). Reading the source, the canonical spelling is **`NSE:NIFTY 50`**
(tradingsymbol with a space): `backend/broker_api/instruments/instruments_repository.py`
maps `NIFTY -> ("NIFTY", "NIFTY 50")` (`normalize_underlying_symbol`) and
`get_spot_token` looks up `segment='INDICES' AND tradingsymbol='NIFTY 50'`.
The constituent interface is separate and unchanged (`source_list` keys
`Nifty50`/`Nifty500`/`NiftyBank` in
`backend/broker_api/instruments/index_ingestion.py`). No constituent or
alert-breadth aggregate was substituted for the index ticker.

## 2. Security mapping

| Capability | Child actions granted | Refused by construction |
| --- | --- | --- |
| `data=true` | `market:read`, `market:stream`, `universes:read`, `universes:resolve` + base run actions | `workflows:*`, universe definition, other owners' universes, dead attempts |
| `data=false` | base run actions only | every market/universe read lens |
| `trade=true` | order actions (`intents:submit`, `runs:exit`, `risk:update`, `proposals:submit`) + `funds:read` | `heartbeat`, `gtt:*`, `runs:create`, `signals:*`, hosted live options mutation |
| `notify=true` | `notifications:publish` | - |

Ownership: hosted reads are scoped to the persisted strategy owner; the broker
`account_scope` never selects an owner. Attempt binding: token id -> job row,
plus status/desired-state/lease checks on every hosted read, so a revoked token
(401), a fenced/stopped/expired attempt (409) or a hosted-shaped token with no
attempt (403) cannot read. Worker-token creation still validates against
`DEFAULT_WORKER_ACTIONS`; the new `universes:*` actions are deliberately **not**
in that owner-issued allow-list, so nothing new appears on the external token
surface.

## 3. Verification (exact commands, exits, results)

All `pytest` runs used the repo venv (`.venv/bin/python -m pytest`) and were
escalated out of the sandbox (async tests hang inside it).

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `pytest tests/api/test_hosted_data_foundation.py -q` | 0 | 23 passed at first submission, **29** after the correction batch (see 3a) - capability truth table; real `prepare_launch` token; HTTP quote/candle/`NSE:NIFTY 50`/indicator/options/universe reads; owner derived from the persisted strategy (not the account scope); `data=false`, `trade=false`, fenced, stopped, expired-lease, orphan-token, revoked-token, cross-owner, definition-mutation and external-contract cases |
| 2 | `pytest tests/strategies/test_readiness.py -q` | 0 | 13 passed at first submission, **18** after the correction batch - syntax/entrypoint/imports/dynamic/optional/profile |
| 3 | `pytest tests/api/test_strategy_readiness_api.py -q` | 0 | 8 passed - route auth, 422s, same-origin refusal, profile in `/options`, and a marker file proving the source was never imported or executed |
| 4 | `pytest tests/strategies/test_supervisor.py -q` | 0 | 31 passed - env allowlist (incl. numeric bounds) and cache-dir creation |
| 5 | `pytest tests/api/test_strategies_api.py tests/api/test_worker_universes.py tests/api/test_hosted_child_authority.py tests/api/test_hosted_proposal_authority.py tests/api/test_hosted_lifecycle_api.py tests/api/test_hosted_live_capabilities.py tests/strategies/test_service.py tests/api/test_algo_worker_route_mounts.py -q` | 0 | 114 passed, 1 failed - the failure is `test_hosted_options_expose_only_authorized_account_scopes`, caused by this workspace's `.env` (`HOSTED_LIVE_ENABLED=true`) being loaded by `backend/broker_api/broker_api.py` at import, not by this change; with `HOSTED_LIVE_ENABLED=false` the same file is **23 passed** |
| 6 | `pytest tests/api/test_algo_worker_api.py tests/api/test_worker_indicators.py tests/api/test_worker_market_depth.py tests/options/test_options_auth_boundaries.py tests/options/test_options_market_routes.py tests/fundamentals/test_fundamentals_routes.py -q` | 0 | 179 passed, 10 failed - all 10 fail identically in a clean `27c58b4` worktree, i.e. pre-existing |
| 7 | `pytest tests/sdk tests/api/test_worker_workflows.py tests/api/test_worker_notifications.py tests/api/test_operator_controls.py tests/api/test_worker_screeners.py -q` | 0 | 326 passed, 2 failed - both fail identically at baseline (`tests/sdk/test_worker_protection_runtime.py`) |
| 8 | `HOSTED_FOUNDATION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' pytest tests/integration/test_hosted_data_foundation_postgres.py -q` | 0 | 5 passed - real `prepare_launch` credential against PostgreSQL; quote/candle/own-universe reads; fenced, stopped, expired-lease and `data=false` refusals; cross-owner invisibility; definition refusal |
| 9 | `HOSTED_FOUNDATION_PG_URL=... pytest tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q` | 0 | 14 passed - lifecycle/concurrency unchanged |
| 10 | `.venv/bin/python scripts/check_worker_sdk_version_refs.py` | 0 | "All worker SDK version references match 0.13.0" |
| 11 | `pytest tests/api/test_worker_contract_fixtures.py -q` | 0 | 1 passed |
| 12 | `ruff check` on the new files | 0 | All checks passed; the legacy files touched contain only pre-existing findings |
| 13 | `docker build -f Dockerfile.supervisor -t kite-algo-strategy-runner:phase1-deps-verify .` | 0 | isolated verification tag only; production tag/container untouched; installed `kite-algo-worker-0.13.0 llvmlite-0.49.0 numba-0.67.0 numpy-2.5.3 pandas-3.0.6 python-dateutil-2.9.0.post0 six-1.17.0` on Python 3.14.7 |
| 14 | `docker run --rm -w /app -e PYTHONPATH=/app -v /tmp/phase1_runner_child_check.py:/tmp/phase1_runner_child_check.py:ro -v /tmp/phase1_runner_spawn_check.py:/tmp/phase1_runner_spawn_check.py:ro kite-algo-strategy-runner:phase1-deps-verify python /tmp/phase1_runner_spawn_check.py` | 0 | child exit 0 through the **real** `spawn_child` path (production `_default_rlimits`, uid/gid 10002, production `_child_env`) |
| 15 | same image, `... python /tmp/phase1_runner_spawn_check_unbounded.py` | 0 | control run with the numeric env keys stripped |

Image evidence (command 14, child log verbatim):

```text
PHASE1_CHILD_CHECK {"bootstrap": {"main_callable": true, "run_child_exported": true, "value": 42},
 "cwd": "/tmp/phase1-child-scratch", "gid": 10002, "uid": 10002, "python": "3.14.7",
 "rlimit_as_bytes": 2147483648, "rlimit_cpu_seconds": 3600, "rlimit_nofile": 256,
 "imports": {"pandas": "3.0.6", "numpy": "2.5.3", "numba": "0.67.0", "numba_threads": 1},
 "indicators": {"numba_available": true, "ema_last": 25149.965421, "rsi_last": 55.381405,
   "supertrend_last": 25141.566773, "candles_to_df_rows": 200}}
```

`rlimit_plan` from the same run: address space 2147483648 (2 GiB), CPU 3600 s,
nofile 256, nproc 128, fsize 268435456 - i.e. the production containment, with
the child dropped to uid/gid 10002 before exec. The env allowlist keys reported
by the harness include `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`,
`NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `NUMBA_NUM_THREADS` and
`NUMBA_CACHE_DIR` (all `"1"` / the scratch path).

Control run (command 15), same limits, numeric keys removed: also exit 0, with
`numba_threads` 10. The explicit thread/cache bounds are therefore *protective
headroom* under a hard 2 GiB address-space cap, not a fix for a reproduced
failure on this workload - and they do not relax containment.

Dependency-choice evidence (official PyPI metadata fetched during this task):
`numba 0.67.0` ships `cp314` wheels and classifies 3.10-3.14; `pandas 3.0.6`
declares `requires-python >=3.11` with `cp314` wheels; `numpy 2.5.3` ships
`cp314` wheels. The platform's own `Dockerfile` already builds the backend on
`python:3.14-slim` with the same unpinned pandas/numpy/numba line, so the runner
profile matches the runtime the server-side indicator service is proven on.

### 3a. Correction batch (root acceptance review)

Four concrete corrections were requested after review and are implemented here;
no earlier edit was reverted and nothing else in the workspace was touched.

1. **Hosted options reads had no action check.**
   `backend/options/api/worker_options_router.py::_hosted_read_guard` now takes
   the hosted decision from `enforce_hosted_read_authority` and, when the caller
   *is* hosted, additionally requires `market:read` (the `data` lens), refusing
   with `HOSTED_OPERATION_NOT_PERMITTED` / `required_action: market:read`.
   External callers still reach the same routes with no action requirement and
   no attempt lookup, so that contract is unchanged.
2. **Streams only authorised at connect.**
   `backend/api/services/market_data.py` adds `_HostedStreamAuthority` and
   `HOSTED_STREAM_AUTHORITY_RECHECK_SECONDS` (default 15 s). The tick loop, the
   reader candle loop and the Redis candle loop re-check the persisted attempt
   on that interval and end the stream with `stream_closed {reason}`; the router
   now passes the token into `stream_candles`, and the two market WebSocket
   routes (`worker_ticks_ws`, `worker_candles_ws`) run the hosted check before
   `accept()` and share the same revalidating generators. External tokens are
   never checked.
3. **`def main(ctx, *, required)` was reported compatible.**
   `_entrypoint_report` now counts required keyword-only parameters and reports
   them `blocked` with the parameter names, because the bootstrap calls
   `main(ctx)` positionally. `def main(ctx, *, note=None)` stays compatible.
4. **Obvious `main` rebinding was reported ready.**
   A second module-level `def main`, or a module-level assignment/reassignment,
   import binding or `del` of `main`, marks the entrypoint check `unknown` and
   holds the overall result at `blocked` with a message naming the ambiguity.
   The response shape is unchanged (the status stays on the check, not on the
   `entrypoint` object), so the accepted Phase-1 contract does not move.

Targeted evidence for this batch (same venv/escalation rules as above):

| Command | Exit | Result |
| --- | --- | --- |
| `pytest tests/api/test_hosted_data_foundation.py -q` | 0 | 29 passed (was 23) - adds chain/Greeks/expiries/mini-chain/PCR/max-pain/selection `403` for an actual lifecycle-issued `data=false` token, the `data=true` counterpart still `200`, SSE tick + candle streams ending with `stream_closed`/`HOSTED_ATTEMPT_FENCED` after a mid-stream fence, inert authority for an external token, and both market WS routes refusing a fenced attempt before `accept()` |
| `pytest tests/strategies/test_readiness.py tests/api/test_strategy_readiness_api.py -q` | 0 | 26 passed (was 21) - adds required keyword-only refusal, optional keyword-only acceptance, `main = None` / duplicate `def main` / `import os as main` / `del main` reported `unknown` with overall `blocked` |
| `pytest tests/api/test_algo_worker_api.py tests/api/test_worker_market_depth.py tests/api/test_algo_worker_route_mounts.py tests/api/test_hosted_child_authority.py tests/api/test_worker_universes.py tests/api/test_worker_notifications.py -q` | 0 | 201 passed - market/SSE/WS route regression; the only touched existing test is the candle-stream stub gaining the `token=None` parameter the route now passes |
| `HOSTED_FOUNDATION_PG_URL=... pytest tests/integration/test_hosted_data_foundation_postgres.py -q` | 0 | 5 passed - persisted authority unchanged on PostgreSQL |
| `pytest tests/options/test_options_auth_boundaries.py tests/options/test_options_market_routes.py tests/options/test_options_resource_behavior.py -q` | 0 | 20 passed, 10 failed - exactly the pre-existing baseline failures, no new ones |
| `ruff check` on the files changed by this batch | 0 | only the two pre-existing findings (`worker_protection.py` unused `WorkerExitRequest`, `market_data.py` unused `Request` import) |

### 3b. Second correction batch (stream credential revalidation + idle-reader bound)

Two gaps in the first streaming correction were reported and are fixed here;
nothing else changed and no earlier edit was reverted.

1. **A revoked token did not end a stream while the job stayed live.**
   `_HostedStreamAuthority` re-checked only the attempt
   (`strategy_jobs` status / `desired_state` / lease), so `revoke_token` without
   job fencing left the stream delivering data until the lease expired - which
   contradicted the documented claim. The authority now also revalidates the
   **credential** on the same bounded interval:
   `SqlAlchemyAlgoWorkerRepository.get_token_status(token_id)`
   (`backend/api/repositories/algo_worker_repo.py:168`, the existing
   reconciliation-evidence lookup) must still return `active`, and the token's
   own `expires_at` - captured at connect, so no secret is needed - must not have
   passed. The lookup is keyed by `token_id`: the raw bearer secret is never
   retained, re-read or logged. Refusals are named
   (`WORKER_TOKEN_REVOKED`, `WORKER_TOKEN_EXPIRED`, `WORKER_TOKEN_UNKNOWN`,
   `WORKER_TOKEN_NOT_ACTIVE`); an unreadable credential ledger ends the stream as
   `WORKER_TOKEN_STATE_UNAVAILABLE` rather than continuing on unverified
   authority. Token-only revocation is representable and detectable in this
   repository, so this is evidence rather than an assumption.
2. **An idle reader stream was not bounded.**
   `reader.stream_candles(...)` was consumed with a plain `async for`, so the
   revalidation only ran *after* the next payload and an idle reader could hold
   the stream open indefinitely. The hosted path now races each read against a
   bounded `asyncio.wait(..., timeout=authority.poll_timeout())`; when the check
   is due it revalidates, emits `stream_closed {reason}` and exits. The in-flight
   read task is cancelled and the reader generator closed on every exit path
   (including client disconnect), so no task or generator is left behind. The
   external path keeps the original straight pass-through byte-for-byte.

Targeted evidence for this batch:

| Command | Exit | Result |
| --- | --- | --- |
| `pytest tests/api/test_hosted_data_foundation.py -q` | 0 | 33 passed (was 29) - adds: tick stream closes with `stream_closed`/`WORKER_TOKEN_REVOKED` after `revoke_token` **while the job row stays `started`, `running` and unexpired**; idle candle reader closes on revocation with the reader generator finalized and no task growth; idle candle reader closes on a fenced attempt; external token keeps the straight pass-through reader (two candle events, no `stream_closed`) |
| `pytest tests/api/test_hosted_data_foundation.py -q -k "stream or revoked"` | 0 | 9 passed - the streaming authority slice on its own |
| `pytest tests/api/test_hosted_data_foundation.py tests/api/test_algo_worker_api.py tests/api/test_worker_market_depth.py tests/api/test_hosted_child_authority.py -q` | 0 | 204 passed - market/SSE/WS route regression around the change |
| `HOSTED_FOUNDATION_PG_URL=… pytest tests/integration/test_hosted_data_foundation_postgres.py -q` | 0 | 5 passed |
| `ruff check` on the files changed by this batch | 0 | only the pre-existing unused `Request` import in `market_data.py` |

## 4. Tested vs unverified

Tested (table above): capability composition; hosted read enforcement on market,
fundamentals, indicator and options read routes; universe read/resolve ownership
and definition refusal; external token contract preservation; readiness route
and helper; child env allowlist; image imports and a representative SDK
indicator (EMA/RSI/Supertrend + `candles_to_df`) as uid 10002 under production
rlimits; PostgreSQL-persisted read authority and lifecycle regression.

Not verified (stated, not implied):

- No live broker or catalog call was made. The canonical index ticker
  `NSE:NIFTY 50` is **source-derived** (repository symbols and `get_spot_token`),
  not confirmed against a running instrument catalog, and constituent lists
  were not re-fetched. The HTTP tests prove the *route* accepts that symbol and
  forwards it to the data service.
- The readiness check cannot certify dynamic imports, guarded code paths or
  strategy correctness - by design, and the response says `unknown`.
- No hosted live options mutation was exercised or unlocked; those refusal rules
  are unchanged from baseline.
- Live trade-data freshness remains a downstream server check; no such server
  was started.
- `NUMBA_NUM_THREADS=1` is a policy choice, not a measured requirement for a
  heavy parallel strategy; only the 200-bar representative workload was run.
- Nothing was deployed: the production strategy-runner image/container
  (`kite-algo-strategy-runner:latest`) is untouched and still runs pre-change
  code.

Pre-existing baseline failures (unchanged, deliberately not fixed):
`tests/options/test_options_market_routes.py` (8),
`tests/options/test_options_auth_boundaries.py` (1),
`tests/sdk/test_worker_protection_runtime.py` (2), and
`test_hosted_options_expose_only_authorized_account_scopes` under this
workspace's `.env` (`HOSTED_LIVE_ENABLED=true`); each reproduces identically at
`27c58b4`.

## 5. Risks and decisions for Astra

1. **`NUMBA_NUM_THREADS=1` (and BLAS/OMP = 1)**: keeps a 2 GiB child comfortably
   inside its cap and does not weaken containment, but caps a strategy's own
   parallelism. The control run shows the representative workload passes without
   the bound, so this is a policy choice - raise it deliberately if a numeric
   strategy needs threads (the address-space limit is the real headroom bound).
2. **`universes:read`/`universes:resolve` are hosted-only**: they are not in the
   owner-issued `DEFAULT_WORKER_ACTIONS` allow-list, so external tokens keep
   `workflows:read`/`workflows:write` exactly as before. Granting the dedicated
   actions to external tokens is a separate contract decision (it would change
   an existing pinned permission test).
3. **Readiness is a contract, not yet wired into version creation**: the route
   and helper exist and are reusable; `POST /strategies/{id}/versions` still
   stores source without a readiness summary (the phase-3 UI consumes the route).
4. **Profile truth lives in two places**: `backend/strategies/readiness.py`
   declares the package list while `sdk/python/pyproject.toml` and
   `Dockerfile.supervisor` install it. A future change to one must update the
   other; noted here instead of adding a build-time generator.

## 6. Checkpoint (if this run is interrupted)

Done: capability/action composition, hosted read authority and route coverage,
universe ownership/definition rules, runner image dependencies + child env
bounds + cache dir, readiness helper/route/SDK types, tests (SQLite + HTTP +
PostgreSQL), isolated image build and in-image spawn verification, this report.
Not started: phases 2-5 (explicit execution authorization, unified first-run UI,
scheduling/approvals UI, representative end-to-end examples). Nothing is
committed or deployed; the working tree holds the Phase-1 diff plus pre-existing
user changes (`documents/hosted-strategies-architecture-r1.md` modified;
untracked docs and `.commandcode/` untouched).

Verification helper scripts (not repository files): `/tmp/phase1_runner_child_check.py`,
`/tmp/phase1_runner_spawn_check.py`, `/tmp/phase1_runner_spawn_check_unbounded.py`.
