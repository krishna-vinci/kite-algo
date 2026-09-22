# Hosted live release — Phase 2C frontend/SDK supported-mode flow

Date: 2026-09-22. Baseline `806e71f`. Worktree `/tmp/kite-hosted-live-ui`, branch
`codex/hosted-live-ui`. **No commit, no push, no deploy, no production API write,
no real order and no notification.** This bundle owns `frontend-next/**` plus the
SDK mode check; `HOSTED_LIVE_ENABLED` and every backend surface are untouched.

## What shipped

| Surface | Change |
| --- | --- |
| `frontend-next/lib/hosted-strategies/types.ts` | `HostedStrategyOptions` gains optional `live_lanes?: string[]` and `live_requires_owner_approval?: boolean`. Both are **optional on purpose**: an older server that omits them is "unknown capability", which is not the same as "live is unsupported" or "no lanes exist" |
| `frontend-next/features/strategies/lib/modes.ts` | **New.** Mode semantics for the hosted surface: labels, `supportedExecutionModes`/`isModeSupported`/`liveModeSupported`, lane labels (`cnc`→CNC / portfolio, `mis`→MIS, `futures`→Futures / rolls, `options`→Options), `liveLaneSummary`, `liveRequiresOwnerApproval`, `preferredCreateMode`, `blockingJob`, `runNowGate` and the operator copy for both gate reasons |
| `frontend-next/features/strategies/components/hosted-strategies-list-page.tsx` | Create form starts on `paper` whenever the deployment offers it; the mode list is labelled from the server vocabulary; a selected mode the server stopped offering is kept (marked "not offered here") instead of silently rewritten; live selection explains real orders, owner approval and the supported lanes; the paper-only claim is gone; the strategies table labels the stored mode and marks one this deployment does not offer |
| `frontend-next/features/strategies/components/hosted-strategy-detail-page.tsx` | Run now shows the strategy's pinned mode as a labelled badge (`Live` is visually distinct), the modes this deployment offers, and — for live — the supported lanes plus the mandatory owner-approval notice. Run now is disabled with a reason that names either the server capability gap or the blocking attempt's status |
| `frontend-next/features/strategies/components/hosted-job-detail-page.tsx` | Attempt mode rendered through the same label helper (`mode: Live`) |
| `frontend-next/vitest.setup.ts` | jsdom lacks pointer-capture and `scrollIntoView`, which Radix Select reads on open; the polyfills let a test open a select (the existing `ResizeObserver` polyfill is the precedent) |
| Tests | `features/strategies/lib/modes.test.ts` (new), `hosted-strategy-detail-page.test.tsx` (new), extended list-page and job-detail tests, and two SDK tests pinning live-mode acceptance |

## Contract consumed

`GET /api/strategies/options` keeps `account_scopes`, `execution_modes`,
`job_kinds`, `stale_exit_policies` and adds `live_lanes: string[]` (empty while
live is disabled, `cnc`/`mis`/`futures`/`options` when enabled) and
`live_requires_owner_approval: bool`. The endpoint stays app-auth,
server-authorized by scope. The UI derives everything from the response:

* `live` is offered **only** when the server lists it in `execution_modes`; the
  browser never adds it, and never treats a strategy row as proof of support.
* Lanes are rendered from `live_lanes`. An empty or absent list is reported as
  "not reported by this server", never as "no lanes supported".
* Owner approval is mandatory unless the server explicitly says otherwise
  (`live_requires_owner_approval !== false`); an older server without the field
  is treated as approval-gated.

## Semantics pinned by tests

1. **First-time default is paper, not live.** `preferredCreateMode` returns
   `paper` whenever it is offered, then `dry_run`, and otherwise `paper` again —
   a live-only (or empty) deployment starts on `paper`, which the form marks "not
   offered here" and which the server refuses. The offered list is never
   auto-selected when it is live, whatever its order.
2. **Run-now gating distinguishes capability from job status.** A strategy pinned
   to a mode this deployment does not offer gets "Live execution is not enabled on
   this deployment…"; an attempt the store would refuse a replacement for gets
   "Attempt #N is running…" / "…is in recovery and is not reconciled…". The client
   mirror uses `replacement_blocked` and the store's own status rule
   (`queued`/`starting`/`running`, unreconciled `recovery_required`); `fencing` is
   deliberately **not** treated as blocking because the store does not.
3. **No silent mode fallback.** The run-now mode is the strategy's own persisted
   mode, read from the strategy row — never re-derived from the options query — so
   an options refresh that stops offering live leaves the strategy showing `Live`
   and blocked. The create form keeps a stored selection that is no longer offered
   and marks it, instead of rewriting it.
4. **A disabled-mode strategy stays visible.** The list still renders it, labelled
   `Live (not offered here)`, and the detail page shows why Run now is unavailable.
5. **Nothing is approved here.** The change is wording plus a disabled control: no
   approval endpoint is added or called, no "auto approve" affordance exists, and
   the live notice states that this deployment never approves a plan
   automatically.
6. **Existing flows keep their error visibility.** Launch refusals still surface
   through `hostedErrorMessage` toasts (pinned by a test that rejects the launch
   with a structured `STRATEGY_BLOCKED`); the disabled button is an addition, not a
   replacement for the server's answer.
7. **An unknown capability holds the launch.** Until `/options` answers, and if it
   fails, Run now is disabled with a "waiting for the supported modes" / "could not
   be loaded" reason. The browser never launches against a capability it cannot
   prove; the server remains the launch authority either way.

## Acceptance correction (root review, 2026-09-22)

Two narrow fixes, no scope change:

* `preferredCreateMode` no longer falls back to the first offered mode. It returns
  `paper` when paper is offered, `dry_run` when only that is offered, and `paper`
  otherwise — a live-only deployment starts on the marked `paper`, never on live.
  Regressions added for `["live"]` and for order `["live","paper"]`, plus a
  component test that the live-only create form shows `Paper (not offered here)`
  and the "does not currently offer" warning.
* `runNowGate` now holds the launch while the capability is unknown: a loading
  `/options` query and a failed one both disable Run now with a distinct reason
  (`modeCapabilityState` maps the query onto `ready`/`loading`/`unavailable`).
  Tests cover the loading and failed cases for the helper and the failed case end
  to end on the detail page.

Re-run after the correction: `npm run typecheck` (exit 0), scoped `eslint`
(exit 0), `NODE_ENV=test npx vitest run features/strategies` (**6 files, 41 tests
passed**). The heavier checks below were not repeated, per the correction's scope.

## Deliberate non-changes

* No new route, API call or state: the surface still uses the existing
  `/api/strategies/*` wrappers, unchanged.
* Existing paper/dry-run strategies, selections and schedules are untouched; no
  mode is edited for an existing strategy (the UI has no mode editor, and this
  bundle does not add one).
* No trading-approval workflow is added; the owner-approval path is unchanged.
* No redesign: labels, gate reasons and one conditional item in an existing select.
* **No SDK source change and no version bump** (see below).

## Executed checks

`npm run typecheck`, scoped `eslint` and the scoped vitest command were re-run
after the acceptance correction; the full-suite, full-lint, build and SDK rows are
from the same revision except for the correction's test-only additions (noted
inline).

| Command (in `/tmp/kite-hosted-live-ui/frontend-next` unless noted) | Exit | Result |
| --- | --- | --- |
| `npm ci --prefer-offline` (escalated: registry access) | 0 | 577 packages installed **in this worktree only**; the main workspace `node_modules` is untouched |
| `npm run typecheck` (`tsc --noEmit`) | 0 | clean |
| `NODE_ENV=test npx vitest run features/strategies` | 0 | **6 files, 41 tests passed** (after the acceptance correction) |
| `NODE_ENV=test npx vitest run` | 1 | **400 passed, 2 failed** — both failures are pre-existing (below). Measured before the acceptance correction; the correction only adds tests to `features/strategies`, covered by the scoped row, so the full suite was not re-measured |
| `npx eslint .` | 1 | 3 errors, 18 warnings, all in files this bundle does not touch: `components/bottom-dock.tsx:19` (conditional hook) and `components/workspace/workspace-provider.tsx:261,287` (`set-state-in-effect`) |
| `npx eslint features/strategies lib/hosted-strategies vitest.setup.ts` | 0 | clean (touched paths) |
| `NODE_ENV=production npm run build` (escalated: the build fetches Google Fonts) | 0 | Next.js 16.2.2 build succeeded, 29 routes, `/strategies` and `/strategies/[strategyId]` present |
| `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/sdk -q` (worktree root) | 0 | **271 passed, 1 skipped** (includes the two new live-mode tests) |

### Pre-existing failures (not caused by this bundle)

`tests/reference-pages.test.tsx > renders the primary strategies operator
workspace` (expects `/strategies workspace/i`) and
`tests/secondary-pages.test.tsx > renders the paper tab inside the shared
strategies workspace` (expects a `paper` tab and a paper strategy). They assert an
older `/strategies` workspace layout, while `app/(app)/strategies/page.tsx` has
rendered the hosted-strategy list since `806e71f`. Verified identical at baseline
by running those two files from a pristine `806e71f` tree
(`git archive 806e71f frontend-next` into `/tmp/kite-baseline-check`, sharing only
the installed `node_modules`): both fail with the same matcher errors.

Logs are outside the worktree, in `/tmp/kite-hosted-live-ui-logs/`
(`vitest-full-final.log`, `eslint-full.log`, `next-build.log`, `sdk-pytest.log`);
no secrets or environment dumps are captured.

## SDK check (no change, no bump)

`execution_mode` is a plain `str` throughout the SDK — `AlgoWorkerConfig`,
`RunConfig`, `create_run`, `async_client.create_run`, `hosted.build_context` — with
no allowlist to extend, and `sdk/python/README.md` already documents
`dry_run, paper, or live`. Nothing in the SDK rejects `live`, so the correct change
is none: the version stays `0.13.0` and `test_package_version_matches_sdk_pyproject`
still guards the parity. Two tests now pin the behaviour that mattered:

* `tests/sdk/test_attach_run.py::test_attach_run_accepts_a_live_run_and_a_live_config`
  — a live run and a live `RunConfig` pass the attach consistency check (a
  paper/dry-run-only vocabulary would have refused a live child here).
* `tests/sdk/test_hosted_bootstrap.py::test_build_context_pins_live_mode_from_the_child_environment`
  — `KITE_ALGO_MODE=live` reaches `ctx.execution_mode` and the attach config.

## Boundaries and integration notes

* The baseline backend in this worktree still returns `execution_modes` including
  `live` unconditionally; gating it by `HOSTED_LIVE_ENABLED` and adding
  `live_lanes`/`live_requires_owner_approval` belongs to the backend worker. The
  frontend test fixtures carry the agreed contract, and the UI tolerates the fields
  being absent, so this bundle is a no-op for a deployment that has not shipped
  them (except that live would look offered — the server still refuses the launch
  and approval).
* This bundle enables nothing: launch, admission, reservation and approval stay
  server-enforced. A client-side mis-state only ever produces a server refusal.
* Run now is disabled while an attempt is `queued`/`starting`/`running` (the store
  would refuse a second attempt). The same-key replay affordance therefore only
  applies while the local job list has not yet learned about the attempt — after a
  lost response the operator reaches the attempt through the jobs table instead of
  the button.
* The frontend image must be rebuilt from the released revision to pick this up.
* Not covered here: the plan-approval UI (still absent from `frontend-next`;
  approval is by API), the schedules UI, and an actual browser session against a
  running backend — no production API was contacted.
