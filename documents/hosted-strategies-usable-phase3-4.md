# Hosted strategies - Phases 3 and 4 (first-run UI and operation) report

Date: 2026-09-23. Status: **ready_for_review** (Astra owns acceptance; nothing
here is self-accepted). Task `/root/hosted_strategy_ui`, workspace
`/home/krishna/kite-algo`, baseline `27c58b4` plus the accepted Phase-1 and
Phase-2 working tree (SDK `0.14.0`, migration `20260923_000042`).

Implements the frontend half of
`documents/hosted-strategies-usable-platform-plan-2026-09-23.md` against
`docs/agent-work/hosted-usable-platform/UI-CONTRACT.md`, `PRODUCT.md`,
`DESIGN.md`, and the Phase-2 handoff in
`documents/hosted-strategies-usable-phase2.md`. No commit, stage, push, deploy,
production migration, broker order, notification, environment or permission
change was made. The only PostgreSQL used is the disposable instance on
**15433** (unique database per run, dropped afterwards); port 15432 was never
contacted.

## 1. What changed

### One-screen creation (`/strategies/new`)

`frontend-next/features/strategies/components/hosted-strategy-composer.tsx`
replaces the multi-step "create strategy, then register a version, then run"
sequence with one page whose section order is the order of the decision: source,
permissions, account and environment, authorization, inputs, timing, then a
compact final meaning above the single primary action.

- **Source first**: paste editor plus a real `.py` file input and a drop target.
  A non-Python file or a file above the server's own 256 KiB bound is refused
  with the reason, and the current source is kept. "Insert starter" pastes the
  tested starter. The uploaded code is never executed in the browser or in the
  API: the only thing that touches it is the `ast` readiness check.
- **Background readiness**: debounced 700 ms and request-sequence guarded, so a
  slow answer for an older source cannot overwrite the newer result, and editing
  the source immediately invalidates what was shown. `unknown` (dynamic imports)
  is a distinct state from `ready`, and a blocked source disables the primary
  action.
- **Permissions by purpose**: "Read market data", "Propose trades", "Send
  notifications", each with what it lets the strategy do, and the statement that
  importing or scanning the code grants nothing.
- **Paper/live is separate from review-first/automatic**: the environment is an
  account choice (`Paper account (no real orders)` / `Live account (real
  orders)`), the authorization a separate two-option decision. Live is offered
  only when the server's `/options` reports it, with the server's own reason when
  it is not.
- **Owner-entered limits only**: selecting automatic trading reveals the
  admission-limit fields, empty by default. Submitting without any limit is
  refused with "Automatic trading needs your own limits".
- **Ordinary schema fields**: parameters start empty (a parameterless strategy
  needs no schema and no JSON), fields are added as name/type/required/range
  entries, and a JSON Schema editor is offered for complex shapes, with an
  explicit note when a schema cannot be shown field-by-field.
- **Retry-safe write sequence**: create strategy -> register version -> set mode
  -> record limits -> issue grant -> queue launch. Every identity already
  obtained is kept, so a failure in a later request resumes from that step
  instead of creating a second strategy or a duplicate version. A lost response
  is only ever resolved to an existing row that matches **every** default this
  page is showing (strategy) or the exact source, schema and permissions
  submitted (version); anything else is refused with an explanation. Generated
  idempotency keys are never rendered. (Corrected in section 6, item 2 - the
  first pass adopted a same-named strategy on fewer comparisons.)

### Operation on the strategy page

The strategy page keeps the existing shell and adds four panels, all reading the
Phase-2 operator contracts:

- **Review or automate** (`hosted-authorization-panel.tsx`): mode switch, the
  active grant with the version/account/environment/limits it is bound to, grant
  issuance behind an explicit summary ("Authorizes v1 of ... on kite:paper in
  Paper account, inside: allocation (INR) 250000, until you revoke it."), the
  grant history, and revocation with the wording that revocation stops later
  dispatch and does not cancel an order a broker already holds. Nothing is issued
  by rendering the page.
- **Execution requests** (`hosted-execution-requests-panel.tsx`): durable
  requests with the request status, the executor's own outcome word
  (`submitted` / `accepted` / `filled` / ... / `uncertain`), named refusals in
  readable copy that keeps the raw code, manual vs automatic decisions, approve
  and reject, and an explicit explainer that queued is not running and dispatched
  is not filled. Expanding a request shows the frozen plan's legs (through the
  new plan-by-id lookup), the reservation/approval links, and "Check admission
  and margin" as a preview that writes nothing.
- **Schedule** (`hosted-schedule-panel.tsx`): create/edit/enable/disable, the
  cadence in words, the server's own next occurrence, the last occurrence with
  its skipped/expired reason, the runtime's misfire grace and overlap policy, and
  an exchange+segment session check that reads the operator calendar. A disabled
  schedule is described as starting nothing, and re-enabling cannot restart a
  disabled strategy (the API refuses it by name).
- **Strategy book** (`hosted-exposure-panel.tsx`): the attributed strategy book
  per environment with unresolved rows shown, and the statement that an empty
  list is not a flat account.

Stop-versus-flatten and log timing are already stated on the job detail page by
the earlier accepted slice ("Stop requests bounded local cleanup. It does not
cancel orders or flatten positions.", and the `post_termination` log source with
its notice). This bundle reads and relies on that wording rather than rewriting
it, and adds no dummy Cancel/Flatten control.

### Minimal missing operator API (same owner/origin/account boundaries)

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/api/strategies/{id}/schedule` | stored schedule, or `null` |
| PUT | `/api/strategies/{id}/schedule` | create or edit (re-pins version/account/policy snapshots) |
| POST | `/api/strategies/{id}/schedule/enabled` | disable / re-enable (refuses a disabled strategy) |
| GET | `/api/strategies/{id}/schedule/occurrences` | materialised occurrences (fired/missed/expired) |
| GET | `/api/strategies/calendar` | exchange sessions for the operator's schedule screen |
| GET | `/api/strategies/{id}/plans/by-id/{plan_id}` | one frozen plan by its own id |

`backend/strategies/scheduling.py` gained `next_occurrence` (the forward mirror
of `due_occurrences`, same kind/timezone/clock rules) and the `OVERLAP_POLICY`
constant, so the "next run" the UI shows comes from the runtime's own rule rather
than a second implementation. `backend/strategies/repository.py` gained
`save_schedule`, `set_schedule_enabled` and `list_schedule_occurrences`.

### Tested starter

`frontend-next/features/strategies/lib/starter.ts` holds the starter source; the
same string is (a) pasted by the composer, (b) parsed by the server's readiness
endpoint and run against a stub context in
`tests/strategies/test_hosted_starter_source.py`, and (c) run through the real
child entry point against a real loopback API, a lifecycle-issued child
credential and a faked quote provider in `tests/api/test_hosted_starter_first_read.py`.

It is **data-only**: it reads one index ticker through the supported SDK client
(`ctx.client.get_quotes`), reports the price through `ctx.progress`, needs no
instrument token, no strategy identity and no hidden parameter, and fits the
permission the composer starts with. The `symbol` parameter is optional (the
ticker default is in the file). Trading examples - proposals, approval waits,
position and pending awareness - belong to the campaign's example phase, where
each waiting rule can be shown with the contract it depends on; the earlier
trading starter was replaced after review (item 4 below).

## 2. Verification (exact commands and results)

This table records the **first pass**, before the review corrections in section
6; the superseding counts are in section 6.8.

Every pytest run and the browser harness were **escalated out of the sandbox**
(the async harness hangs inside it, as Phases 1-2 recorded). Exit codes are the
real process status.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `NODE_ENV=test .venv/bin/python -m pytest tests/strategies/test_schedule_next_occurrence.py -q` | 0 | 7 passed - forward rule for daily/weekly/monthly/calendar, month clamping, exhausted calendar, and agreement with `due_occurrences` |
| 2 | `NODE_ENV=test .venv/bin/python -m pytest tests/strategies/test_hosted_starter_source.py -q` | 0 | 4 passed - the shipped starter is `ready`, proposes/requests once, refuses repeats, and reports missing configuration |
| 3 | `NODE_ENV=test .venv/bin/python -m pytest tests/api/test_hosted_schedule_api.py -q` | 0 | 8 passed - session/origin/owner scoping, create+edit on one row, next/last occurrence, kind validation, disable/re-enable with `STRATEGY_DISABLED`, occurrences |
| 4 | `NODE_ENV=test .venv/bin/python -m pytest tests/api/test_hosted_schedule_api.py tests/api/test_strategies_api.py tests/api/test_strategy_readiness_api.py tests/api/test_hosted_execution_requests.py tests/strategies/test_schedule_next_occurrence.py tests/strategies/test_hosted_starter_source.py -q` | 1 | 93 passed, 1 failed - **the single failure is the pre-existing environment-dependent case** `test_hosted_options_expose_only_authorized_account_scopes`, which reads this workspace's `.env` (`HOSTED_LIVE_ENABLED=true`); row 5 isolates it |
| 5 | `HOSTED_LIVE_ENABLED=false NODE_ENV=test .venv/bin/python -m pytest tests/api/test_strategies_api.py -q` | 0 | 23 passed - the same file with the environment artifact removed |
| 6 | `.venv/bin/python -m ruff check <changed backend files + harness>` | 0 | `All checks passed!` (one dead `StrategyCreateRequest` import in the touched router was removed) |
| 7 | `cd frontend-next && NODE_ENV=test npx tsc --noEmit` | 0 | clean |
| 8 | `cd frontend-next && NODE_ENV=test npx eslint features/strategies lib/hosted-strategies "app/(app)/strategies"` | 0 | clean on every file this bundle touches |
| 9 | `cd frontend-next && NODE_ENV=test npx eslint .` | 1 | 3 errors in two **unmodified** files (`components/bottom-dock.tsx` conditional hook, `components/workspace/workspace-provider.tsx` setState-in-effect) plus pre-existing warnings - red at HEAD for the same reason |
| 10 | `cd frontend-next && NODE_ENV=test npx vitest run` | 0 | 51 files, **431 passed** - including 24 new tests (composer 6, authorization 3, execution requests 5, schedule 4, schema 6) |
| 11 | `cd frontend-next && NODE_ENV=test npx next build` (network escalation for Google Fonts) | 0 | production build succeeds; `/strategies/new` is in the route list |
| 12 | `.venv/bin/python documents/verification/hosted-usable-phases3-4-2026-09-23/harness.py` | 0 | isolated browser pass, 17 screenshots, all scenario assertions true, database dropped |

### Browser pass (row 12)

`documents/verification/hosted-usable-phases3-4-2026-09-23/` holds the harness,
its `harness-result.json` and the screenshots. Provenance:

- database: disposable `kite_ui_qa_<random>` on `127.0.0.1:15433`, migrated with
  `alembic upgrade head` (through `20260923_000042`), dropped afterwards;
- API: the real `/api/auth` and `/api/strategies` routers over loopback, with the
  market boundary faked (one synthetic price) - no broker, no notifications;
- frontend: the real `next dev` server (port 3300) using its existing `/api`
  rewrite, with the operator origin on the deployment-style allowlist;
- browser: headless Chrome 146 driven over CDP with real DOM input events.

Assertions recorded by the run (all true): readiness ready; keyboard focus in the
source editor; a blocked source disables the primary action; a dropped `.txt` is
refused with the typed source preserved; a schema parameter added through the UI;
limit fields start empty; the strategy is created and queued in review-first
mode; a grant is issued from the owner's own limit and then revoked; a real
pending request reads "Waiting for your decision"; the frozen plan's legs render;
the schedule saves with next run `2026-09-24T10:15:00+00:00` (15:45 IST the day
after the run, i.e. the forward rule is correct); the schedule disables.

Screenshots (first pass; the correction pass adds `09`, `20` and renumbers the
rest - see the verification README for the current list): composer, strategy page
and authorization, execution request and plan review, schedule
form/saved/disabled, and the narrow-width pair.

One flake was found and fixed while verifying: the two composer tests that wait
on the debounced readiness answer used a 3 s bound, which loses under a fully
parallel 51-file run. They now allow 15 s, and three consecutive full runs are
green (431 passed each).

## 3. Tested vs unverified

Tested: source validation and readiness states (ready/blocked/unknown/error),
stale-answer protection, file type and size refusal with source preservation, the
generated schema round trip, the parameterless path, the retry-safe
create/version/configure/launch sequence, the summary wording, the authorization
panel's no-grant-on-load behaviour and grant/revoke flow, execution request
states and decisions, plan review, schedule next/last/missed/overlap/disable, the
operator calendar's named unavailable answer, owner/origin scoping on the new
routes, and the whole flow in a browser against the real API.

Not verified (stated, not implied):

- **No live broker order, fill or settlement.** The market boundary is faked in
  the QA harness and the pending execution request is a synthetic row written by
  the harness in the platform's own table shape; it exercises the operator
  surface, not a child run. Approving or rejecting that synthetic request is
  therefore *not* claimed as proven end to end.
- The production app shell is a fixed desktop layout (68 px rail + fixed top
  bar, and `frontend-next/app/layout.tsx` sets no viewport meta). Measured in the
  harness's narrow-width pass (`mobile=True` would let Chrome pick a wider layout
  viewport and hide the problem, so the pass asks for a real 390px layout
  viewport): the shell's own top bar has a 443px minimum and its bottom dock
  437-616px, which sets the document to 511-684px - **so this bundle does not
  claim a mobile pass.** Within that, the composer's own content has a 285px
  minimum (fits), and the strategy page's is 469px, down from 612px after the
  correction pass: its cards now shrink to the column and the wide tables scroll
  inside their own card instead of widening the page
  (`versions_table_inside_card` in `harness-result.json`). Making the shell itself
  responsive is a shell change, explicitly out of scope (`DESIGN.md`: preserve
  the shell).
- The dispatcher is disabled in the QA harness, so "queued -> dispatching ->
  executed" was not observed live; it is covered by Phase-2's suites and by the
  panel's wording.
- Nothing is deployed: the running containers still execute pre-change code, and
  the new operator routes and UI exist only in the working tree.

## 4. Decisions and risks for Astra

1. **Two stale frontend tests were rewritten.** `tests/reference-pages.test.tsx`
   and `tests/secondary-pages.test.tsx` asserted a "strategies workspace" with
   live/paper tabs that no longer exists anywhere in the app (verified against
   `HEAD`: neither the text nor the tabs are in the committed page). They now
   assert the current page's contract (heading, the `/strategies/new` entry
   point, the registered-strategies section). No assertion was weakened; revert
   just those two hunks if you would rather keep them red.
2. **`APP_ALLOWED_ORIGINS` is the QA harness's only API configuration change.**
   It names the dev-server origin because the browser talks to the dev server,
   which proxies `/api`. Nothing in the product changed.
3. **Plan review needed a lookup.** A durable execution request records `plan_id`
   but not `proposal_id`, and the existing plan route keys on `proposal_id`.
   Rather than adding a field to the Phase-2 request contract, this bundle adds
   `GET /{id}/plans/by-id/{plan_id}`. If you prefer the request row to carry
   `proposal_id`, that is a one-line additive change in the Phase-2 service.
4. **Superseded by the review correction: the platform no longer stamps identity
   into parameters.** The first pass had the composer and schedule panel add the
   strategy id to the launch/schedule params. That is wrong twice: it breaks a
   strict (`additionalProperties: false`) schema - `tests/api/test_hosted_launch_params.py`
   shows the API refusing such a key with 422 - and it reserves a parameter name
   that belongs to the author. Launch and schedule now send exactly the values the
   operator entered. A trading example that needs the canonical identity must get
   it from the persisted SDK/run contract instead; that is Phase 5.
5. **Superseded by the review correction: a parameterless strict schema launches
   as-is.** With no platform-stamped key there is nothing that forces a loose
   schema, so a version may be declared `additionalProperties: false` with no
   properties and launched with `params={}` (same test file).
6. **`get_db_connection()` readers and `DATABASE_URL`.** The operator calendar
   route opens its own connection from `DB_*` (the same pattern as the existing
   worker calendar route), which the harness points at the disposable database. A
   deployment whose `DB_*` and `DATABASE_URL` disagree would surface the same
   split here as elsewhere.

## 5. Checkpoint

Done: the composer route and its seven sections; readiness (debounced,
sequence-guarded); file input/drop validation; the tested data-only starter;
schema fields with a JSON fallback; permissions and authorization by purpose;
owner limits; the retry-safe write sequence with a hidden key; the four operation
panels; the minimal schedule/calendar/plan-by-id operator API with tests; the
hidden retry identity on Run now; the list page's `/strategies/new` entry point;
the seven review corrections in section 6; the browser harness, its result file
and 20 screenshots in `documents/verification/hosted-usable-phases3-4-2026-09-23/`
(the disposable database for the recorded run is named in `harness-result.json`;
every run creates a fresh one and drops it).

Not started: Phase 5 (runnable examples, the guide, and the full
process/API/disposable-PG integration proof). Nothing is committed or deployed;
the working tree still holds the Phase-1/Phase-2 diff and the pre-existing
unrelated changes.

## 6. Correction pass - the seven review items

Reviewed against the first pass at actual code and screenshots. Every item below
is fixed in the working tree; the evidence is the command in section 6.8 that
covers it.

1. **Composer parameters are launch inputs.** The detail-page Run now form and
   the composer now share `features/strategies/components/hosted-params-editor.tsx`
   and `features/strategies/lib/schema.ts`: the pinned version's schema is
   rendered as ordinary fields (required with no default, `false`/`0`, enums,
   ranges), a schema that cannot be shown losslessly falls back to a JSON box, and
   `runHostedStrategy` sends exactly the operator's values - the platform no
   longer injects `strategy_id` (or anything else) into them. The default starter
   needs no hidden parameter.
2. **Partial creation resumes from immutable snapshots.** Each completed step
   records the exact inputs it was bound to (`strategyFingerprint`,
   `versionFingerprint`, `policyFingerprint`, `grantFingerprint`,
   `launchFingerprint`). Editing the source, schema or permissions registers a NEW
   version and drops every key minted for the old one; switching autonomous ->
   review-first calls `setAuthorizationMode` back; changing the owner's limits
   re-records the policy and mints a new grant request. A lost create response is
   adopted only when every default on the page matches the stored strategy, and a
   lost version response only when the stored revision's source, schema and
   permissions are exactly the ones submitted; anything else is refused with an
   explanation, never silently adopted.
3. **Readiness is the answer about the current source, not a single status.**
   `classifyReadiness` treats `status=ready` with any `unknown` check (dynamic or
   guarded optional imports) as "partly verified - not certified ready", which
   needs the operator's explicit acknowledgement; `checking`, `error`, `idle` and
   a stale answer for a different source all disable the primary action. An
   oversized paste is refused in the browser (256 KiB, the server's own bound)
   before any request is made.
4. **The starter is a data-only example that actually reads something.** It reads
   an index ticker through the supported SDK and reports the price, fits the
   default permission, and is proven against the real API with a lifecycle-issued
   credential and a faked quote provider
   (`tests/api/test_hosted_starter_first_read.py`). No trading quickstart is
   shipped half-wired.
5. **Copy and limits.** Live copy now says what the server says: a live attempt
   needs the OWNER's authority, either a decision on that plan or the standing
   authorization they issue - never the platform's own consent
   (`liveRequiresOwnerApproval`, `modes.ts`). The two lanes are only offered when
   the strategy can trade, and when the selected version cannot, the
   authorization panel says so. Review-first now records the owner's admission
   limits too (previously only automatic trading did), and neither lane invents a
   number.
6. **Layout.** The source editor is bounded (`h-[18rem] max-h-[45vh]` + internal
   scroll) instead of pushing the settings off-screen; the readiness and Run-now
   alerts are single-child so inline `<code>` no longer lands on its own row; the
   strategy page lets each card shrink and scroll its own table; stored times in
   tables render as short local values (`formatTimestamp`) instead of raw ISO
   tokens. Narrow-width evidence is in the browser row below, including what is
   still a shell limit.
7. **Enabling a schedule re-checks everything a launch does.** `POST
   /{id}/schedule/enabled` re-runs the deployment live flag, the pinned
   account/mode authorization and the version-belongs-to-strategy check before it
   re-enables (a disabled schedule stays possible). Covered by
   `tests/api/test_hosted_schedule_api.py`.

### 6.8 Verification for this correction pass

Every pytest run and the browser harness were **escalated out of the sandbox**
(the async harness hangs inside it). Exit codes are the real process status.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `cd frontend-next && NODE_ENV=test npx tsc --noEmit` | 0 | clean |
| 2 | `cd frontend-next && NODE_ENV=test npx eslint features/strategies lib/hosted-strategies "app/(app)/strategies"` | 0 | clean |
| 3 | `cd frontend-next && NODE_ENV=test npx vitest run` | 0 | 51 files, **445 passed** (composer 16, authorization panel 5, format, schema, schedule, execution requests, pages) |
| 4 | `HOSTED_LIVE_ENABLED=false NODE_ENV=test .venv/bin/python -m pytest tests/api/test_hosted_launch_params.py tests/api/test_hosted_schedule_api.py tests/api/test_hosted_starter_first_read.py tests/strategies/test_hosted_starter_source.py tests/strategies/test_schedule_next_occurrence.py tests/api/test_strategies_api.py tests/api/test_hosted_execution_requests.py tests/api/test_hosted_lifecycle_api.py tests/api/test_strategy_readiness_api.py -q` | 0 | **120 passed** |
| 5 | `.venv/bin/python documents/verification/hosted-usable-phases3-4-2026-09-23/harness.py` | 0 | isolated browser pass, **20 screenshots**, every recorded assertion true, disposable database dropped |

Rows 1 and 4 need the environment note recorded in section 2 row 4: this
workspace's `.env` sets `HOSTED_LIVE_ENABLED=true`, so
`tests/api/test_strategies_api.py::test_hosted_options_expose_only_authorized_account_scopes`
fails unless the run pins the deployment default (`HOSTED_LIVE_ENABLED=false`).
That test and the `.env` are untouched by this bundle.

The browser row's own record (`harness-result.json`) asserts, in addition to the
first-pass scenarios: a refused `.txt` drop shows its notice AND does not refuse
the launch (`refused_drop_notice_shown`, `refused_drop_does_not_block_launch`);
the review-first lane shows the owner's own limits
(`review_first_limits_visible`); Run now is a schema field, not a JSON box
(`run_now_params_are_fields`); the authorization panel names a version that
cannot trade (`authorization_notes_no_trade_capability`).
