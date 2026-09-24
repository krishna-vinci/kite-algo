# Hosted usable platform: release preflight and Nifty-500 momentum adapter plan

Current integration status: the platform release was committed as `e23be53` and
deployed, with migration `20260923_000042`; see
`documents/hosted-usable-deployment-2026-09-24.md`. The momentum example is accepted
only for isolated paper experimentation, with current evidence and unresolved
recurrence restrictions in `documents/hosted-momentum-closure-2026-09-24.md`.
The preflight inventory and commands below are a historical preparation record,
not an instruction to repeat the deployment or stage every listed file again.

Date: 2026-09-24. Baseline `27c58b4` (`development`). Deployed code `f0c747c`;
production database at `20260922_000041` with the new revision `20260923_000042`
unapplied.

This bundle is **read-only investigation plus this document and the staging
manifest**. Nothing was staged, committed, built, pushed, migrated, deployed,
reconfigured or executed as a real order or notification. No repository file
other than the two new documents was written. No 429 was encountered, so no retry
or model substitution was needed.

**Addendum, 2026-09-24 (later the same day).** Sections 7.3, 7.7.4, 7.8.4 and the
closing paragraph are superseded by `documents/hosted-momentum-closure-2026-09-24.md`
and by the inline corrections below. Since the preflight: the momentum adapter
gained its API/disposable-PostgreSQL harness and four recorded green scenarios,
the execution-request wait contract was corrected (`awaiting_approval` is not a
terminal state), and PAPER orders now exist inside disposable databases. Nothing
was committed, pushed, deployed or migrated, no real order and no notification
was sent, and the deployment UI is unchanged.

Sources read: `docs/agent-work/hosted-usable-platform/CHECKPOINT.md`,
`documents/hosted-strategies-usable-platform-plan-2026-09-23.md`, the four phase
reports (`-phase1`, `-phase2`, `-phase3-4`, `-phase5`),
`examples/hosted_platform/USER-GUIDE.md`,
`documents/hosted-strategies-live-deployment.md`, the compose overlays, the
campaign diff, and the supplied momentum source at
`/home/krishna/.codex/attachments/d54cecb3-d1a7-4153-bc59-80d1a4ea656e/Pasted text.txt`.

## 1. Verified preflight facts

| Fact | Evidence |
| --- | --- |
| The whole campaign (plus this bundle) is uncommitted on `development` @ `27c58b4` | `git status --porcelain -uall` = 237 entries (61 modified, 176 untracked) at the time the manifest was generated |
| Only this worktree holds that work | `git worktree list` — the other worktrees (`daily-candle-finalization`, `development-daily-finalization`, `sdk-0.7.6`, `/tmp/kite-hosted-strategy-foundation`) sit at unrelated revisions |
| Production DB head is `20260922_000041`; the three `hosted_execution_*` tables do not exist | `select version_num from alembic_version`; `information_schema.tables like 'hosted_execution%'` -> NONE |
| The new revision is additive and single-headed | `backend/alembic/versions/20260923_000042_hosted_execution_authorization.py:36` (`down_revision = "20260922_000041"`) |
| The API entrypoint owns the migration | `compose.yml:123` — `alembic -c /app/backend/alembic.ini upgrade head && uvicorn ...` |
| Four services run from this repo; two are touched by the campaign | `docker ps` -> `kite-app`, `kite-alerts-worker`, `kite-frontend-next`, `kite-strategy-runner` all `Up 38 hours (healthy)`; `kite-market-runtime`, `kite-postgres`, `kite-redis` are older and untouched |
| The hosted execution dispatcher is **on unless explicitly disabled** | `backend/strategies/execution_dispatcher.py:22-38` — only a falsy spelling (`0/false/no/off/disabled`) disables it |
| The runner installs the SDK from the repo, not PyPI | `Dockerfile.supervisor:42-46` — `pip install "/tmp/sdk[indicators]"` |
| Live hosted strategies still cannot be configured | `.env` `HOSTED_STRATEGY_ACCOUNT_SCOPES` has exactly **1** entry and it is a paper scope (`kite:pa...`, length 12); `HOSTED_LIVE_ENABLED=true` |

## 2. Scoped staging manifest

`documents/hosted-usable-release-manifest-2026-09-24.txt` — 237 paths in three
explicitly delimited sections. The sections are bounded by `# >>> SECTION n
BEGIN` / `# <<< SECTION n END` markers so a parser never has to count lines:

| Section | Count | Meaning |
| --- | --- | --- |
| 1 | 125 | Required campaign surface: backend, migration, SDK, frontend, tests, phase reports, guide, examples, the momentum adapter and its tests, this plan |
| 2 | 92 | `EVIDENCE_OPTIONAL`: the phases-3/4 browser harness plus 20 screenshots, and the `examples/hosted_platform/evidence/*.json` runs |
| 3 | 20 | **Do not stage**: `.commandcode/` (5), the pre-existing modified `documents/hosted-strategies-architecture-r1.md`, and the architecture drafts/artifacts (`architecture-flow.md`, `hosted-strategies-architecture-r3.md`, `hosted-strategies-implementation-roadmap.md`, `hosted-strategies-proposal-draft.md`, `documents/kite-algo-platform*` (9 incl. PNG/JSON visual-check outputs), `openalgo-*.md` (2)) |

Staging command (Section 1 only, marker-delimited):

```bash
awk '/^# >>> SECTION 1 BEGIN/{f=1;next} /^# <<< SECTION 1 END/{f=0} f && /^[^#]/' \
  documents/hosted-usable-release-manifest-2026-09-24.txt \
  | xargs -d '\n' git add --

# Section 1 has 125 paths and none of the excluded ones.
git diff --cached --name-only | wc -l
git diff --cached --name-only \
  | grep -E '^\.commandcode/|architecture-r1|kite-algo-platform|openalgo|architecture-r3|implementation-roadmap|proposal-draft|architecture-flow' \
  | wc -l        # must be 0
git commit -m "feat(strategies): hosted usable platform (data foundation, governed execution, composer, scheduling, examples)"
```

The earlier draft header suggested `grep -v '^#' FILE | xargs git add --`, which
would have staged sections 2 and 3 as well. That instruction is replaced by the
marker parser above; sections 2 and 3 must never reach `git add`.

Two manifest cautions:

1. `docs/` is git-ignored (`.gitignore:48`), so the campaign's own working notes
   under `docs/agent-work/hosted-usable-platform/` (`CHECKPOINT.md`,
   `AUTHORIZATION-CONTRACT.md`, `PHASE2-BRIEF.md`, `UI-CONTRACT.md`,
   `EXAMPLES-CONTRACT.md`, `PRODUCT.md`, `DESIGN.md`) are **not** in the manifest
   and cannot be committed without a force-add. Treat them as working notes or
   force-add them deliberately.
2. `scripts/*` is git-ignored except two allowlisted files, so no campaign helper
   script is stageable without an explicit `.gitignore` change.

## 3. Evidence secret scan (no values printed)

Scanned the campaign surface — the modified diff's added lines, every changed
path except the 5 `.commandcode/` entries (232 paths), `examples/hosted_platform/**`,
`documents/verification/**` and the four phase reports — for AWS keys, PEM private
keys, `sk-` tokens, JWT-shaped strings, `postgres://user:pass@` DSNs and
`key = "<24+ chars>"` assignments. Re-run after the momentum adapter, its schema,
its tests and the guide/plan edits: no hits.

Result: **no production secret found.** The only two credential-shaped hits are
the disposable test DSN with the literal password `testonly`:

```
documents/hosted-strategies-usable-phase1.md:postgresql://postgres:testonly@127.0.0.1:15433/kite_test
documents/verification/hosted-usable-phases3-4-2026-09-23/harness.py:postgresql://postgres:testonly@127.0.0.1:15433/postgres
```

`harness.py` additionally sets `APP_JWT_SECRET=ui-qa-secret-not-for-any-other-environment`
and blanks `APP_ADMIN_PASSWORD_HASH*` for its isolated run — synthetic values, not
the deployment's. `.env` is git-ignored and appears in no manifest section; its
`HOSTED_SUPERVISOR_CREDENTIAL`, `HOSTED_LIVE_ENABLED` and scope allowlist never
reach the evidence.

## 4. What the release touches in production (read-only inventory)

Queried with `docker exec kite-postgres psql ...` — no credentials printed,
account identities masked.

| Surface | State now | Effect of this release |
| --- | --- | --- |
| `alembic_version` | `20260922_000041` | -> `20260923_000042` (additive; applied by the `finance-app` entrypoint) |
| `hosted_strategies` | 2 rows: `My first hosted strategy` (`active`, paper, finite) and `mcx-data-only-acceptance` (`disabled`, paper) | new `authorization_mode` column defaults to `approval_based`; no row changes meaning |
| `hosted_strategy_versions` | 2 | untouched |
| `hosted_strategy_schedules` | 0 (enabled 0) | untouched; the new schedule UI has nothing to list |
| `strategy_jobs` | 3 rows, **one non-stopped**: `hsj_4088ffb5...` paper, `desired_state=started`, `status=recovery_required`, `reconciled_at=NULL`, `recovery_required_at=2026-09-23 06:31:56Z`, `run_id=run_9594eede...` | Not restarted: the supervisor claims only `status="queued"` (`backend/strategies/supervisor.py:327`) and `recovery_required` is not claimable. It will keep showing as an unreconciled attempt until an operator reconciles it. **Do not reconcile it during the deploy.** |
| `algo_worker_runs` (non-terminal) | 17: paper 5, dry-run 7, live 5 (`status` open 13 / closed 4). The `live` rows and most dry-run rows are legacy (April–August) | untouched; recreated containers do not re-claim them |
| `live_plan_executions` / `live_plan_submissions` / `strategy_plan_execution_events` | 0 / 0 / 0 | untouched |
| `live_order_intents` / `canonical_order_events` / `signal_events` (24 h) | 0 / 0 / 0 | untouched |
| `account_positions` | 0 | untouched; broker flatness is explicitly **not** asserted from it |
| `strategy_position_projection` | 0 rows | the hosted book snapshot reports `coverage="unknown"` (`backend/strategies/execution_snapshot.py:18-44`) |
| `worker_live_execution_links` | 40 | untouched |
| Dispatcher | absent from the deployed code | **starts with the new code**; 0 governed requests exist because the tables are created empty |

No live execution request exists to preserve or stop: the request tables are
created by this migration, so adoption begins from zero. Nothing in this bundle
was altered.

## 5. Executable release steps

Written to be run in order after this preflight; each step names its exit
criterion.

### 5.1 Pre-flight (read-only, immediately before the commit)

```bash
git -C /home/krishna/kite-algo rev-parse HEAD            # expect 27c58b4
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'kite-(app|frontend-next|strategy-runner|alerts-worker)'
docker exec kite-postgres psql -U postgres -d postgres -tAc "select version_num from alembic_version"
docker exec kite-postgres psql -U postgres -d postgres -tAc \
  "select id,status,desired_state,reconciled_at from strategy_jobs where desired_state <> 'stopped'"
docker exec kite-postgres psql -U postgres -d postgres -tAc "select count(*) from live_plan_submissions"
```

Exit criterion: `20260922_000041`, the same single `recovery_required` job, 0
submissions, all four containers healthy.

### 5.2 Stage and commit (exactly the manifest)

The Section 2 command above, then verify the staged set against the manifest, then
commit on `development`. Do **not** `git add -A`.

### 5.3 Validate the committed tree before building

```bash
.venv/bin/python -m pytest tests/strategies/test_execution_authorization.py \
  tests/strategies/test_execution_dispatcher.py tests/api/test_hosted_execution_requests.py \
  tests/api/test_hosted_execution_bypasses.py tests/strategies/test_readiness.py -q
HOSTED_LIVE_ENABLED=false NODE_ENV=test .venv/bin/python -m pytest \
  tests/api/test_strategies_api.py tests/api/test_hosted_schedule_api.py \
  tests/api/test_hosted_launch_params.py tests/api/test_strategy_readiness_api.py -q
cd frontend-next && NODE_ENV=test npx tsc --noEmit && NODE_ENV=test npx vitest run
.venv/bin/python scripts/check_worker_sdk_version_refs.py     # expect 0.14.0
```

Exit criteria are the recorded campaign results (131 targeted, 120 hosted HTTP,
445 frontend, SDK refs matched). These are regression checks on the committed
tree, not a re-acceptance of the phases; the phase reports carry the full tables
(`documents/hosted-strategies-usable-phase2.md:329`,
`documents/hosted-strategies-usable-phase3-4.md:349`,
`documents/hosted-strategies-usable-phase5.md:186`).

This step deliberately excludes the broad `tests/api/**` sweep: parts of that
suite hang in this workspace, the campaign already recorded which files hang, and
re-running it adds wall time without evidence about this change. Run the targeted
suites above plus the momentum bundle
(`tests/strategies/test_nifty500_momentum_source.py`,
`tests/sdk/test_hosted_bootstrap.py`, `tests/strategies/test_readiness.py`) and
stop there unless a specific concern names another file.

### 5.4 Build images before replacing anything

```bash
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  build finance-app alerts-worker strategy-runner frontend-next
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedSince}}' \
  | grep -E 'kite-algo-(finance-app|alerts-worker|strategy-runner|frontend-next)'
```

`strategy-runner` must be rebuilt: `Dockerfile.supervisor` now installs the SDK
`[indicators]` extra (pandas/numpy/numba) and the child env adds the numeric
thread bounds. `frontend-next` must be rebuilt to gain the `/strategies/new` route
and the composer.

### 5.5 Migrate (owned by the API container) and roll out

```bash
# 1. API first: its entrypoint runs `alembic upgrade head`, then serves.
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d --no-deps finance-app
docker logs --tail 50 kite-app | grep -E '20260923_000042|Application startup'

# 2. Only after the API is healthy: the rest.
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d --no-deps alerts-worker strategy-runner frontend-next
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml ps
```

`kite-postgres`, `kite-redis` and `kite-market-runtime` are not recreated and no
volume changes. The migration is additive; **downgrade is not a rollback path** —
it fails while governed rows exist (fix-forward, per the migration's docstring).

Each replacement is an implicit stop+start for that one service.
`kite-strategy-runner` currently runs **no child** (the only non-stopped job is
`recovery_required` and is not claimable), so the restart ends no strategy
execution.

### 5.6 Post-deploy verification (read-only)

```bash
docker exec kite-postgres psql -U postgres -d postgres -tAc "select version_num from alembic_version"
docker exec kite-postgres psql -U postgres -d postgres -tAc \
  "select count(*) from hosted_execution_grants; select count(*) from hosted_execution_requests"
# expect 20260923_000042, then 0 and 0
docker exec kite-postgres psql -U postgres -d postgres -tAc \
  "select column_default from information_schema.columns where table_name='hosted_strategies' and column_name='authorization_mode'"
# expect approval_based
docker exec kite-postgres psql -U postgres -d postgres -tAc \
  "select count(*) from strategy_jobs where desired_state <> 'stopped' and reconciled_at is null"
# expect 1 (the pre-existing job, unchanged)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18777/api/strategies    # expect 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:13000/strategies        # expect 307 -> /login
docker logs --tail 20 kite-app | grep -i hosted_execution_dispatcher
# expect a starting/running component status, not degraded
```

Then, authenticated in the product UI (Section 6): `/strategies/new` renders the
paste-or-drop composer, readiness answers for the shipped starter, and
`/strategies` lists the existing strategies with the pre-existing job still
visible as an unreconciled attempt.

### 5.7 Not performed by any of the above

No real order, no notification, no live strategy creation, no schedule
enablement, no `.env` change, no `HOSTED_STRATEGY_ACCOUNT_SCOPES` change, no PyPI
publication, no production reconciliation of the pre-existing `recovery_required`
job, and no `live` account-scope expansion. Creating an autonomous grant or a live
account scope remains a separate, explicitly approved action.

Authorization scope for this release, as clarified by the owner on 2026-09-24:
**paper autonomous grants and paper autonomous execution are authorized** (the
first three review-first/autonomous paper runs described in the campaign), and
that authorization does not extend to live mode. Live stays blocked twice over -
`HOSTED_LIVE_ENABLED=true` with a single **paper** entry in
`HOSTED_STRATEGY_ACCOUNT_SCOPES`, so no broker scope is offered - and widening
that allowlist or creating a live strategy is a separate decision that this plan
does not take.

## 6. Browser test and operator authentication

Two distinct paths, both already exercised by the campaign.

**Isolated QA (no production data).**
`python documents/verification/hosted-usable-phases3-4-2026-09-23/harness.py`
creates a disposable `kite_ui_qa_<uuid>` database on **15433**, migrates it with
`alembic upgrade head` (so it already includes `20260923_000042`), serves the real
`/api/auth` and `/api/strategies` routers over loopback with a faked market
boundary, runs the real `next dev` server on `127.0.0.1:3300` and drives headless
Chrome over CDP. It authenticates through the app's own login route —
`POST /api/auth/login` with a same-origin cookie (`harness.py:648-668`) — using
credentials the harness sets itself (`APP_ADMIN_USERNAME`/`APP_ADMIN_PASSWORD`,
`APP_ALLOW_INSECURE_DEV_AUTH=true`), and blanks the workspace's production admin
hash so no real secret is read. Its env pins
`HOSTED_STRATEGY_ACCOUNT_SCOPES=kite:paper` and
`HOSTED_EXECUTION_DISPATCH_ENABLED=false`, so the QA pass cannot dispatch.

**Production smoke (operator, after deploy).** Browser -> `http://<host>:13000`
-> `/strategies` redirects to `/login?next=%2Fstrategies`. This requires one of:
an already-authenticated operator session, or the operator's plaintext
credential as the product login form expects it. The deployment stores only
`APP_ADMIN_PASSWORD_HASH*` in `.env`, so the password is **not recoverable from
the repository or the environment** and this document does not claim the login
is reproducible from the workspace alone. Do not mint an operator cookie, do not
call the login endpoint with a synthesized password, and do not set an insecure
dev-auth flag on the deployment: an unauthenticated browser check stops at the
redirect, and the authenticated walkthrough is the operator's own action (or
root's, if root holds a session). `APP_ALLOWED_ORIGINS` must name the frontend
origin for the same-origin assertion to pass; no token is minted by hand and
nothing is printed.

Checklist for that authenticated pass: composer readiness for the starter; the
blocked path for a file without `main(ctx)`; parameter fields; the permission
summary; both authorization modes; the grant/revoke panel; a pending execution
request rendering as "waiting" (not running); and the schedule form's session
check. Every one of these has a recorded assertion in `harness-result.json`, and
the unauthenticated half (401 on `/api/strategies`, 307 on `/strategies`) is
checkable without a credential.

## 7. Nifty-500 momentum strategy: adapter preflight

The attachment is 727 lines of dependency-free statistical logic: 8 constants, 7
frozen dataclasses, 2 `StrEnum`s and 7 module-level functions ending in
`build_monthly_momentum_plan(...)` returning a `PortfolioPlan`. It is a pure
library — **no `main(ctx)`**, no SDK import, no I/O. Its constants
(`LOOKBACK_SESSIONS=252`, `TOP_N=15`, the 200/150 SMA windows,
`BREADTH_THRESHOLD=0.40`, `MAX_POSITION_PERCENT=0.10`,
`EQUAL_WEIGHT_CAP_MULTIPLIER=1.50`) are strategy parameters; this plan preserves
them exactly and optimizes nothing.

### 7.1 Blocker A — the file cannot be loaded by the production loader

Measured in the real runner (`kite-strategy-runner`, Python 3.14.7) through the
production entrypoint:

```
python -c "from kite_algo_worker.hosted import load_strategy_main; load_strategy_main('/tmp/momentum_source.py')"
LOAD_FAILED AttributeError 'NoneType' object has no attribute '__dict__'
```

Root cause: `load_strategy_main` builds the module with `spec_from_file_location`
+ `module_from_spec` + `exec_module` and never registers it in `sys.modules`
(`sdk/python/kite_algo_worker/hosted.py:96-110`). `@dataclass` then resolves a
string annotation via `sys.modules.get(cls.__module__).__dict__` and finds `None`.
`from __future__ import annotations` (which stringifies every annotation) or a
quoted annotation (`regime: "RegimeState"`, line 88 of the attachment) triggers
it. Bisected on the actual file:

| Variant | Result |
| --- | --- |
| file as supplied | `AttributeError` |
| remove `from __future__ import annotations` only | `AttributeError` (quoted `regime` remains) |
| unquote `regime: "RegimeState"` only | `AttributeError` (`__future__` stringifies the rest) |
| remove `from __future__` **and** unquote `regime` | loads; then `RuntimeError: hosted strategy must define a callable main(ctx)` |
| unchanged source, loader registering the module in `sys.modules` | loads cleanly (verified with an equivalent standalone probe) |

**Decided and implemented (owner, 2026-09-24):** the SDK fix. `load_strategy_main`
registers the module in `sys.modules` under `spec.name` before `exec_module` and
restores the previous entry (or removes the name) if the import fails, so a
half-built module is never left behind:

```python
module_name = spec.name or "hosted_strategy"
previous = sys.modules.get(module_name)
sys.modules[module_name] = module
try:
    spec.loader.exec_module(module)
except BaseException:
    if previous is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = previous
    raise
```

Evidence: `tests/sdk/test_hosted_bootstrap.py` now loads a real
`from __future__ import annotations` + `@dataclass` + `StrEnum` source through the
loader, asserts the class is usable and its fields resolvable through
`dataclasses.fields`, and asserts both failure paths (no leftover module; a
restored previous entry). The adapter itself is loaded the same way by every test
in `tests/strategies/test_nifty500_momentum_source.py`. The strategy source keeps
its `from __future__ import annotations` import, so the owner-supplied math is
unchanged apart from the approved regime branch.

### 7.2 Blocker B — no entrypoint, and readiness will say `blocked`

The file has no `main(ctx)`, so `assess_source_readiness` refuses it before
anything is created and the composer's primary action stays disabled. That is
correct behaviour, not a defect: the adapter must add `def main(ctx)` to the same
file.

One file, not two: the supervisor materializes exactly one source file per attempt
at `<workspace>/source/<job_id>/<version_id>.py`, hash-checked against
`source_sha256` (`backend/strategies/supervisor.py:299,636-647`), and the child is
invoked as `python -m kite_algo_worker.hosted <source_path>` (line 630). A sibling
module cannot be shipped and `ctx.scratch` is per-attempt, not durable. So the
preserved math must be inlined into the single adapter file. Consequence: any
later edit to the math changes the source hash, creating a new immutable version
and invalidating a standing autonomous grant (the platform's documented
re-authorization rule).

### 7.3 Data coverage measured against production (read-only)

| Requirement | Measured |
| --- | --- |
| Index ticker identity | the catalog spells it `NSE:NIFTY 50` (token 256265) and `NSE:NIFTY 500` (token 268041), with a space |
| Index history | NIFTY 500 = 1263 daily bars, NIFTY 50 = 1240; latest bar `2026-09-22T18:30Z` = the 2026-09-23 IST session |
| Constituents | `Nifty500` = 500 rows, **all** with an `instrument_token`; `Nifty50` = 50 |
| 253-session alignment | across the most recent 253 distinct daily sessions (`2025-09-18` to `2026-09-23` IST): **480 of 500** constituents have all 253; 481 have at least 250 |
| Breadth substrate (200 sessions) | 493 of 500 constituents have at least 200 bars, so a real breadth denominator exists today |
| Trading-session calendar | `exchange_calendar_sessions` NSE/CM = 2246 sessions valid to 2026-12-31, reachable through `GET /worker/market/calendar` (`backend/api/routers/worker_market.py:229`, `sdk/python/kite_algo_worker/client.py:726`) |

History-window caution: `get_candles(..., interval="day", lookback=N)` is the live
cache path and the backend's own default is 365 *days* (`market_data.py:485`),
which is not enough for 253 sessions plus holidays. The adapter must use
`ctx.client.get_historical_candles(instrument, timeframe="day", from_date=...,
to_date=...)` with an explicit range (hard ceiling 3650 days,
`market_data.py:143-162`). The latest bar carries session-finality metadata rather
than a per-candle flag: the history response is annotated with
`last_candle_final` / `complete` / `completeness_reasons`
(`worker_market.py:111-130`, `exchange_calendar.assess_daily_completeness:231-260`),
so the adapter derives "completed session" from that metadata and refuses when the
last bar is not final.

Corrected 2026-09-24 (see `documents/hosted-momentum-closure-2026-09-24.md`): the
implemented rule is the verified calendar's own session close plus the platform's
900 s finality delay, or the platform's `last_candle_final` verdict when the
session IS the newest expected one. A calendar row with no reported close is
never treated as finished on the strength of its date; the as-of session falls
back to the newest provably finished session and the run defers by name. The
strategy's `validate_history` still rejects any candle carrying
`final=False`.

Membership is the catalog's **current** constituent list; the platform keeps no
point-in-time membership, so the window carries survivorship bias as a stated
limitation rather than a defect.

### 7.4 Contract mapping (strategy input to actual platform call)

| Strategy model | Platform source |
| --- | --- |
| `Candle(session, close, final)` | `get_historical_candles(symbol, timeframe="day", from, to)`; `close` from the candle payload, `session` from the IST date of `ts`, `final` from the completeness metadata |
| `trading_sessions: Sequence[date]` | `get_market_calendar(from, to, exchange="NSE", segment="CM")` — never a hard-coded NSE calendar |
| `member_histories` | `get_index_constituents("Nifty500")`, resolve each constituent, then one history call each |
| `index_history` | the NIFTY 500 index history (token 268041) |
| `current_prices` | daily closes from the same completed bars (the plan's `reference_price`), optionally cross-checked with `get_quotes` |
| `current_holdings` | `ctx.run.owned_work()["positions"]` — **strategy-owned**, never account net |
| `strategy_fund` | the owner-issued admission allocation; the proposal either omits `capital_basis_inr` (the normal path) or states it exactly, otherwise `CAPITAL_BASIS_MISMATCH` / `CAPITAL_BASIS_INVALID` refuse before any plan exists |
| `previous_regime` | recomputed deterministically from the same index history (7.6) — no new state API, no scratch |
| identity | `ctx.run.attribution()` from the persisted binding; never a parameter |
| submission | `ctx.run.submit_proposal(...)` then `ctx.run.request_execution(plan_id, idempotency_key=...)`; review-first parks it as `awaiting_approval`, autonomous runs it under the matching grant |

Two honest coverage rules the adapter must honour, both platform facts:

1. `owned_work()` reports `coverage="unknown"` when the strategy's position
   projection is unpublished or stale (`execution_snapshot.py:18-44,229-277`), and
   production has **0** published rows. Unknown is not flat, so deltas computed
   from an unknown book would be guesses; the adapter must refuse to plan rather
   than assume a zero book.
2. Pending quantity that already covers an intended change means "do not send it
   again" (the platform's duplicate-observation rule, restated in
   `examples/hosted_platform/USER-GUIDE.md` section 8).

### 7.5 Regime semantics — resolved by the owner on 2026-09-24 (implemented)

The supplied `evaluate_regime` used `trend_strong or breadth_strong ->
CAUTIOUS(0.50)`, i.e. a passing index trend supplied half exposure even when
breadth failed. That is now explicitly overridden:

```
breadth_strong = breadth >= 0.40                  # members above their own 200-session SMA
trend_strong   = index_close > index_sma          # 200-session SMA when previously RISK_ON,
                                                  # 150-session SMA when previously CAUTIOUS/DEFENSIVE

if not breadth_strong:   DEFENSIVE, exposure 0.00  # no entry, and EXIT existing holdings
elif trend_strong:       RISK_ON,   exposure 1.00
else:                    CAUTIOUS,  exposure 0.50
```

The index-trend logic, including the 200/150 asymmetry, is retained exactly where
it still applies — when breadth passes. Only the failed-breadth branch changes: it
now means zero exposure regardless of the index trend.

Cadence, also resolved: breadth is evaluated on **completed daily sessions** and a
failed breadth exits existing holdings that day, while **selection and rebalance
remain monthly** on the configured rebalance session. Practically that needs one
daily schedule (after session close plus the platform's 900 s finality delay)
whose run decides monitor-only versus rebalance by asking
`RebalanceSchedule.is_due(...)`.

Execution-mode consequence (root explained, owner accepted): with the strategy in
**review-first** mode a daily exit becomes an execution request that waits for the
owner's approval; in **autonomous** mode the same request is admitted under the
version-bound grant. The strategy needs no second code path, but the operational
meaning of a breadth exit differs by mode and must be stated in the strategy's own
documentation.

### 7.6 Durability of `previous_regime` without inventing state

The asymmetric window makes the current state a function of the previous state,
but the platform exposes no durable per-strategy key/value store for runtime
state, `ctx.scratch` dies with the attempt, and the brief forbids both an invented
generic state API and ephemeral storage for durable state. The strategy is also a
pure function library and holds no state itself.

What was investigated: the platform's durable evaluation records
(`strategy_proposals` / `strategy_plans`, keyed by `evaluation_id`) persist the
proposal payload and the frozen plan, and `ctx.run.execution_requests()` exposes
this run's own requests. None of them is a strategy-readable "previous regime"
contract: a child cannot enumerate its own prior proposals, no column stores the
regime decision, and the run-bound reads are per-attempt. So there is no safe
persisted-state contract to read, and inventing one is out of scope.

Resolution implemented: an explicit ``initial_regime`` parameter plus a **bounded
pure replay of the full data** from a fixed configured ``regime_anchor_date``.
Each replayed session uses only the index closes and constituent closes up to and
including that session, then applies the same ``evaluate_regime`` the live path
uses - so the state carried into the anchor is whatever the previous day's
*breadth and trend* produced.

Two shortcuts were explicitly rejected and are **not** implemented:

* replaying the index series alone (invalid: the previous breadth gate is part of
  the state, so the 200/150 choice would drift from the real trajectory);
* any sliding "last 200 sessions" window (it re-seeds the state every run and
  destroys the asymmetry the strategy defines).

The replay is bounded by ``regime_replay_max_sessions`` (default 260, hard cap
750): an anchor further back than the bound refuses by name
(``REGIME_REPLAY_TOO_LONG``) instead of silently shortening the window. Coverage
is required across the WHOLE replay window plus its 200-session warm-up, not just
the tail - that is what "complete as-of coverage" means here. Fetching starts a
fixed 400 calendar days before the anchor (never "today minus N"), so the same
anchor selects the same window on every run.

If a durable per-strategy regime column is wanted later, that is new platform
capability and belongs in its own approved bundle; the replay above is the honest
interim answer and needs no state infrastructure.

### 7.7 What was delivered

1. `sdk/python/kite_algo_worker/hosted.py` — the `sys.modules` registration and
   its failure-path restore (7.1), with new coverage in
   `tests/sdk/test_hosted_bootstrap.py`.
2. `examples/hosted_platform/nifty500_momentum.py` — one file containing the
   preserved math plus the hosted adapter. The two approved deviations are the
   failed-breadth branch and nothing else; the parameter defaults are
   `index_symbol="NSE:NIFTY 500"` (the catalog spelling), `constituents_source=
   "Nifty500"`, `rebalance_kind="MONTHLY_LAST_SESSION"`,
   `initial_regime="DEFENSIVE"`, `coverage_policy="fail_closed"`.
3. `examples/hosted_platform/nifty500_momentum.schema.json` — the parameter
   contract, including the truth that `budget_inr` and `regime_anchor_date` are
   required and that `fail_closed` is the default coverage policy.
4. `tests/strategies/test_nifty500_momentum_source.py` — 53 tests that load the
   real source through the real loader and cover: the regime matrix including the
   failed-breadth override and the retained 200/150 asymmetry; the 253-session
   ranking requirement; the position cap and whole-share allocation; the
   exact-quantity `intent_bundle` submission (integer targets, no weights);
   15-name target selection; the no-change no-op; the daily breadth exit
   (zero targets, no notional, intent `breadth_exit`); off-schedule recovery
   placing nothing; unknown book ⇒ named no-action with a preview and no
   submission; unreadable constituent ⇒ refusal (never an exclusion); gapped
   history under both coverage policies; late-listed member exclusion; pending
   work deferral; non-CNC refusal; finite-positive capital; missing anchor;
   replay bound; the platform refusal path (no orders); a named no-action for an
   unreadable index; an unverified session close never becoming the as-of
   session; and the execution-request contract — `awaiting_approval` is not
   terminal, the child holds while the owner decides and exits 0 only on the
   status it observed, a wait that outlives the attempt's bound ends UNRESOLVED
   by name, and exit code 2 for a genuinely broken run.

   Correction, 2026-09-24: the earlier count of 29 predates the harness bundle.
   Current measurement: `.venv/bin/python -m pytest
   tests/strategies/test_nifty500_momentum_source.py -q` → 53 passed, exit 0, and
   94 passed, exit 0, for the four-file command given below.

Run them with:

```bash
.venv/bin/python -m pytest tests/strategies/test_nifty500_momentum_source.py \
  tests/sdk/test_hosted_bootstrap.py tests/strategies/test_readiness.py -q
```

### 7.8 Gaps and decisions that remain

1. **Owner-allocation equality is not child-verifiable on this path.** A hosted
   child has no read of `StrategyAdmissionPolicy.allocation_inr`, and the
   platform's `CAPITAL_BASIS_MISMATCH` refusal is implemented for
   `target_kind="target_weights"` only (`backend/strategies/proposals.py:305`).
   This adapter submits exact-quantity `intent_bundle` legs (which is what
   preserves whole shares), so it can state `budget_inr` and enforce its own
   affordability, but it cannot prove equality with the recorded allocation. The
   binding platform control is the admission ceiling
   (`ALLOCATION_EXCEEDED`, `backend/strategies/admission.py:408-422`), which
   applies to any plan carrying priced legs. Closing the gap is a small platform
   change (extend the capital-basis check to the bundle path, or expose the
   allocation to the child) and is **not** part of this bundle.
2. **`ALLOCATION_EXCEEDED` on an invested book.** Admission counts the
   strategy's existing attributed consumption **plus** the plan's notional, and a
   full-target plan's notional is the whole book. A monthly rebalance of a book
   already near the allocation can therefore be refused even when the trade is
   net-flat in cash. The adapter reports that refusal by name and places nothing;
   the sizing/workaround decision (raise the allocation, or a delta-shaped plan
   contract) belongs to root.
3. **Fan-out cost.** One history call per constituent is ~500 calls per daily
   run. The adapter uses bounded concurrency (`fetch_concurrency`, default 8) and
   derives both the 253- and 200-session windows from a single fetch per symbol.
   A batched history read would be a new platform contract; without one, the
   eligible universe size is the practical limit.
4. **An API/disposable-PostgreSQL paper harness for this adapter now exists and
   has run green.** `examples/hosted_platform/run_phase5_acceptance.py` gained a
   momentum runner with four scenarios: `momentum_mid_month_deferral`,
   `momentum_manual_entry`, `momentum_autonomous_entry` and
   `momentum_breadth_exit`. Each drives the real routers over loopback, the real
   `backend.strategies.supervisor` child, the real governed
   proposal/plan/request/dispatch pipeline and the real paper executor against a
   uniquely named disposable PostgreSQL on 15433; only the market-data source,
   the synthetic constituent snapshot and the verified calendar rows are
   fixtures. Recorded runs: `evidence/phase5-20260924T034828Z.json` (the four
   momentum scenarios, `ok: true`) and `evidence/phase5-20260924T035017Z.json`
   (the whole harness, all ten scenarios, `ok: true`). Full trace, the two root
   causes found while making it green, and the recurrence gaps that remain are in
   `documents/hosted-momentum-closure-2026-09-24.md`.

   Correction, 2026-09-24 (later): those recorded runs predate the freshness guard
   added in that document's section 6 (no silent as-of rollback; an explicit
   ``False`` finality verdict is scoped to a bar after the as-of session). Under
   the guard these fixtures refuse by name (``INDEX_HISTORY_STALE``, evidence
   `examples/hosted_platform/evidence/phase5-20260924T040853Z.json`), because
   their synthetic index history stopped at a fixed past month end while the
   seeded calendar carries verified closes through the real today.

   Resolved the same day (Option A, section 6.4 of the closure record): the
   fixture now takes its as-of session from the platform's own completion rule
   against the real clock, synthesizes history through it, returns the current
   still-open session's bar explicitly when there is one, and the scenarios are
   scheduled explicitly (``MONTHLY_CALENDAR_DAY`` resolving onto the as-of session
   for the two due scenarios, and onto a provably different verified session for
   the deferral). Current evidence for the four momentum scenarios:
   `examples/hosted_platform/evidence/phase5-20260924T041640Z.json` (`ok: true`,
   harness exit status 0), with the input SHA-256s recorded in the file. The
   earlier four-scenario (`...T034828Z.json`) and ten-scenario
   (`...T035017Z.json`) runs are kept as pre-guard records only; the other six
   scenarios were not re-run because this bundle does not touch them. This does
   not make the
   strategy production-recurring-ready (7.8.1-7.8.3 and the closure record,
   section 4).

Recorded but not blocking: constituent membership is current-only (survivorship
bias); the strategy needs explicit `max_days`-aware history ranges (implemented);
the daily schedule must fire after session close plus the platform's 900 s
finality delay; and `evaluation_kind` is submitted as `run_now` because the
accepted examples do the same and the platform derives the rest.

## 8. Documentation defect found during preflight — corrected

`examples/hosted_platform/USER-GUIDE.md:82-99` claimed that "this platform's
catalog stores an index as `NSE:NIFTY50` (unspaced)", which contradicted the live
catalog (`NSE:NIFTY 50`, token 256265) and the campaign's own screenshots
(`symbol = NSE:NIFTY 50`). The guide now states the catalog's real shape - indices
spaced (`NSE:NIFTY 50`, `NSE:NIFTY 500`, `NSE:NIFTY BANK`), equities unspaced -
and keeps the correct advice to resolve the instrument and use
`instrument["public_key"]`.

## 9. Blockers and decisions for Astra

Decisions taken and implemented in this bundle (recorded so they are not reopened):
the loader registers the module in `sys.modules` and restores on failure; the
trend index is **NIFTY 500** (as the source's own `validate_history("NIFTY500",
...)` implies); an unknown book is **never** treated as flat, so the first run of
this strategy is a named no-action until a projection exists; and the regime is
replayed from a configured anchor with an explicit initial state rather than from
an index-only or sliding window.

Open, needing root:

1. Owner-allocation equality on the exact-quantity path (7.8.1) — the adapter
   cannot verify it, the platform does not implement it for `intent_bundle`, and
   the admission ceiling is the binding control today. Decide between extending
   the platform check, exposing the allocation to the child, or accepting the
   ceiling as the control.
2. Rebalance sizing on an invested book (7.8.2) — the double-count that produces
   `ALLOCATION_EXCEEDED`; this is the most likely reason a second monthly run
   produces no orders in practice.
3. Resolved 2026-09-24: the momentum example now HAS its own
   API/disposable-PostgreSQL harness inside `run_phase5_acceptance.py` and four
   recorded green scenarios (7.8.4). What remains open is not coverage but
   production recurrence: the `OPEN_EXPOSURE` reconciliation refusal, the
   `STRATEGY_BLOCKED` gate on the next job while an attempt is unreconciled, and
   the invested-book `ALLOCATION_EXCEEDED` sizing question - see
   `documents/hosted-momentum-closure-2026-09-24.md` section 4.

Not blockers: the index-spelling doc line (Section 8), the `docs/` ignore
behaviour for the campaign's own notes (Section 2), and the mobile-shell overflow
already recorded as a known limitation in the campaign acceptance record.

Nothing here is self-accepted. No commit, build, migration, deployment or
reconciliation was performed.

Corrected 2026-09-24: the momentum work DID place PAPER orders - inside
uniquely named disposable databases on 15433, never against production, and
never as a real broker order. The unit tests still use a stubbed client; the
phase-5 harness runs the real routers, supervisor child, governed pipeline and
paper executor. The disposition of every gap above is recorded in
`documents/hosted-momentum-closure-2026-09-24.md`.
