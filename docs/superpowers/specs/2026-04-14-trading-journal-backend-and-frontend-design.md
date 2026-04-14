# Trading Journal Backend and Frontend Design

Date: 2026-04-14
Status: Approved design draft for backend completion first, then frontend implementation

## Objective

Finish the trading journal system inside the existing Kite Algo app so that phase 1 and phase 2 are fully complete on the backend, then implement a dashboard-first frontend journal workspace in `frontend-next/`.

This design assumes:

- the journal remains part of the same product, not a separate app
- backend completion comes before frontend implementation
- Quick Trade is an entry surface for naked option strategies, not a separate journal family
- benchmark comparison against Nifty50 is required across supported strategy families
- the frontend should use the current Next.js app shell and operator-style UI patterns already present in `frontend-next/`

## Product framing

The journal is the user-facing review and analytics shell for trading activity.

The hidden engine underneath it is the decision/review system that captures:

- why a strategy was entered
- how it evolved
- how rules were followed or violated
- how the result compares against the user’s benchmark and historical process

The system must support all current and planned trading modes without forcing them into a single-trade-only shape.

## Locked domain rules

### Strategy taxonomy

- Every tradeable flow is treated as a strategy-oriented run.
- Quick Trade is not a separate journal family.
- Quick Trade should map into `options_strategy` and may be tagged with `entry_surface = quick_trade` in metadata.

### Supported strategy families

The journal must support:

- `options_strategy`
- `indicator_strategy`
- `investment_strategy`
- `discretionary_strategy`

### Benchmark policy

- Initial benchmark is `NIFTY50`
- Benchmark comparison must be available for all supported strategy families
- Comparison must align by the same analysis window and capital basis

### Phase ordering

- Finish backend phase 1 and phase 2 completely first
- Implement frontend journal workspace after backend contract is stable enough to support it

## Why backend stays in Python for phase 1 and 2

The journal domain should stay in the Python backend for this phase because:

- the existing execution truth sources already live close to the Python backend
- the journal requires transactional access to Postgres-backed execution and runtime data
- the current repo already has a service/repository pattern that fits the journal domain well
- adding Celery would create queue, retry, and worker complexity without solving the core journal-domain problem

Go remains the right place for:

- websocket ownership
- market-data fanout
- hot-path runtime/tick processing

Go is not the right place for journal-domain orchestration in phase 1 or 2.

## Current implementation state that this design extends

Already implemented in the working branch:

- journal schema foundation in `schema.sql`
- `journaling/` package with models, repository, metrics, benchmark helpers, service, and runtime worker
- backend journal router in `api/routers/journal.py`
- operator scripts for journal backfill and metric recompute
- partial phase-2 source attribution hooks in option strategy, paper runtime, algo runtime, and momentum/investment flows
- focused backend tests for repository, metrics, benchmark helpers, service, router, runtime, and source hooks

This design covers what is still required to call phase 1 and phase 2 fully complete.

## Backend completion definition

Phase 1 and phase 2 should be considered complete only when the backend supports all of the following with stable APIs and verifiable data.

### 1. Aggregate journal summaries

The backend must provide real aggregate summary endpoints for:

- day
- week
- month
- year
- since inception

Each summary must be queryable for:

- all strategies combined
- strategy family
- strategy name
- execution mode
- benchmark id

Each summary should include, when supported by data quality:

- run count
- closed run count
- gross profit
- gross loss
- net P&L
- total charges
- win rate
- average win
- average loss
- profit factor
- expectancy
- cumulative return
- benchmark return
- excess return
- max drawdown
- drawdown duration
- Sharpe ratio
- Sortino ratio
- streaks
- review completion rate
- rule adherence rate

Sharpe and Sortino must only be returned when enough periodic equity data exists to make them meaningful.

### 2. Correct equity curve and benchmark foundations

The backend must support trustworthy periodic equity series, not just trade-row summaries.

This requires:

- consistent `journal_equity_points` rebuilding for closed and open runs
- better open-run mark-to-market handling than the current approximate state
- benchmark normalization by window for all subject types
- explicit handling of windows with incomplete equity data

If data is not sufficient for a metric, the API should return a safe null/unsupported shape rather than inventing a value.

### 3. Full lifecycle sync across source systems

The journal must stay aligned with the source systems through the run lifecycle.

#### Options strategy flow

The current create-time mirror must be extended so that:

- status changes propagate
- execution results update journal state
- linked journal runs can be resolved consistently from option strategy records

#### Paper flow

The current journal attribution hooks must be completed so that:

- orders and trades link back to journal runs safely
- basket execution updates remain idempotent
- paper strategy activity contributes to the same summary and benchmark contract as live flows

#### Algo flow

The current decision-event capture must be completed so that:

- trigger decisions are recorded when resolvable
- journaling side-effects never break core algo dispatch
- algo-linked runs remain queryable as first-class journal strategy runs

#### Investment flow

The journal must treat investment activity as strategy cycles or rebalance runs, not isolated holdings.

This requires:

- strategy-cycle grouping keyed by tag/rebalance context
- benchmark comparison against Nifty50 at the cycle level
- meaningful summary behavior for longer-horizon investment runs

### 4. Rules and review completion

The backend must finish the rules/review contract so the frontend can support real workflows.

That includes:

- rule CRUD
- rule evidence attachment to runs
- rule adherence rollups by family/strategy/window
- review updates and completion state changes
- durable storage of decision and review notes
- insight derivation from stored review/rule/decision evidence

The system must preserve the distinction between:

- discovered patterns
- active rules
- rule states such as reinforced/decaying/retired

### 5. Operator and maintenance controls

The backend must expose enough operational visibility for safe use.

Required operator capabilities:

- historical backfill
- metric recompute by calc version
- benchmark freshness visibility
- projection/runtime lag visibility
- dry-run support for destructive or large operations where appropriate

These can live in scripts and/or authenticated backend admin endpoints, but they must exist and be testable.

## Backend route contract

The journal router should stabilize around these route groups.

### Core run routes

- `GET /api/journal/runs`
- `GET /api/journal/runs/{run_id}`
- `POST /api/journal/runs`
- `PATCH /api/journal/runs/{run_id}`

### Review and decision routes

- `POST /api/journal/runs/{run_id}/decision-events`
- `PATCH /api/journal/runs/{run_id}/review`
- `GET /api/journal/review-queue`

### Summary and benchmark routes

- `GET /api/journal/summary`
- `GET /api/journal/benchmark`
- `GET /api/journal/calendar`

### Strategy/rule/insight routes

- `GET /api/journal/trades`
- `GET /api/journal/strategies`
- `GET /api/journal/rules`
- `POST /api/journal/rules`
- `PATCH /api/journal/rules/{rule_id}`
- `GET /api/journal/insights`

### Operator routes or equivalent script-backed support

- recompute metrics
- refresh benchmark data
- backfill journal history
- inspect projection/runtime lag

Exact URL shapes can vary slightly during implementation, but the route surface must cover these capabilities.

## Frontend design direction

Frontend will be implemented in `frontend-next/` after backend completion.

### Chosen landing experience

`/journal` should be dashboard-first.

The default landing page should lead with:

- KPI summary cards
- benchmark comparison
- recent runs
- review queue
- mini calendar lower on the page

### Chosen journal workspace navigation

The journal workspace should include:

- Overview
- Calendar
- Trades
- Strategies
- Rules
- Insights

### Chosen interaction level

The first complete release should be fully working for core journal flows:

- dashboards and filtering
- drilldowns
- review updates
- decision notes/events
- rule management
- benchmark views

This is not a read-only analytics shell.

## Frontend route structure

Create these routes inside the app router:

- `frontend-next/app/(app)/journal/page.tsx`
- `frontend-next/app/(app)/journal/calendar/page.tsx`
- `frontend-next/app/(app)/journal/trades/page.tsx`
- `frontend-next/app/(app)/journal/strategies/page.tsx`
- `frontend-next/app/(app)/journal/rules/page.tsx`
- `frontend-next/app/(app)/journal/insights/page.tsx`

Update app navigation so Journal appears as a first-class destination in the shared shell.

## Frontend component boundaries

The journal UI should follow the existing compact operator layout patterns in `frontend-next/components/operator/*` and the shared shell in `frontend-next/components/app-shell.tsx`.

Recommended new files:

- `frontend-next/lib/journal/types.ts` — API response and view-model types
- `frontend-next/lib/journal/api.ts` — journal API calls and query helpers
- `frontend-next/components/journal/journal-nav.tsx` — page-local journal tabs/subnav
- `frontend-next/components/journal/journal-header.tsx` — title, filters, benchmark chip, period controls
- `frontend-next/components/journal/overview-kpis.tsx`
- `frontend-next/components/journal/benchmark-comparison-card.tsx`
- `frontend-next/components/journal/review-queue-card.tsx`
- `frontend-next/components/journal/recent-runs-card.tsx`
- `frontend-next/components/journal/calendar-heatmap.tsx`
- `frontend-next/components/journal/trades-table.tsx`
- `frontend-next/components/journal/strategy-performance-table.tsx`
- `frontend-next/components/journal/rules-panel.tsx`
- `frontend-next/components/journal/insights-feed.tsx`
- `frontend-next/components/journal/review-drawer.tsx`
- `frontend-next/components/journal/rule-editor-dialog.tsx`

These components should remain relatively focused rather than becoming one giant page-local file.

## UI/UX direction

The frontend should reuse the existing app shell and compact operator panels instead of introducing a visually unrelated section.

### UI recommendation from ui-ux-pro-max

Best fit recommendation for this module:

- style: **data-dense dashboard**
- primary palette: **blue analytics palette with amber highlights**
- typography intent: **technical, precise, analytics-friendly**

Practical adaptation for this repo:

- keep the app’s existing dark operator foundation
- use current panel/KPI styles already present in the dashboard and operator components
- use blue for comparative analytics states and amber for benchmark/review emphasis where useful
- avoid decorative glassmorphism or consumer-style marketing visuals

### Accessibility and interaction rules

- keyboard-friendly filters and tables
- visible focus states
- no emoji icons
- stable hover states with no layout shift
- clear empty states for missing data windows
- explicit unsupported-metric states when a metric is unavailable due to insufficient data

## Using UI Builder and ui-ux-pro-max

`ui-ux-pro-max` should be used for design-system guidance and visual direction.

`ui-builder` should be used only for narrow, file-targeted help because it has low context.

When using UI Builder, prompts should mention exact files and one narrow goal at a time, for example:

- build `frontend-next/components/journal/overview-kpis.tsx` using existing `components/operator/kpi-card.tsx` patterns
- draft `frontend-next/components/journal/calendar-heatmap.tsx` compatible with current app shell spacing and dark theme
- improve `frontend-next/app/(app)/journal/page.tsx` layout hierarchy using existing operator panels

Do not ask UI Builder to redesign the entire journal workspace in one shot.

## Data flow

### Backend-first contract

Frontend implementation must wait until the backend contract is stable enough to support:

- overview KPI summaries
- benchmark comparison series
- review queue data
- recent runs list
- calendar summaries
- trades and strategies list/filtering
- rule management
- insight feed

### Frontend data access

Use the current frontend data patterns already present in `frontend-next/lib/api/client.ts` and related modules.

Recommended approach:

- typed fetchers in `frontend-next/lib/journal/api.ts`
- page-level or component-level data hooks built around existing frontend query patterns
- optimistic UI only where mutation safety is obvious, such as review-state updates or rule edits with easy rollback

## Error handling

### Backend

- journal side-effects must not break core trading or algo execution
- invalid source references should fail safely and log clearly
- unsupported metric states should be explicit, not silently converted to zero
- benchmark gaps should surface as incomplete coverage, not fake normalized values

### Frontend

- each page needs loading, empty, and error states
- metrics unavailable due to insufficient data should show a clear explanation
- mutation failures should surface non-destructively with retry paths

## Testing requirements

### Backend

Add or extend tests for:

- aggregate day/week/month/year summaries
- benchmark comparison alignment
- open-run equity rebuild behavior
- option/paper/algo/investment lifecycle sync
- rule CRUD and evidence rollups
- review queue and insight generation
- operator backfill/recompute safety

### Frontend

Add tests for:

- journal route rendering
- navigation presence
- overview KPI and benchmark cards
- calendar page rendering
- trades/strategies/rules/insights route smoke tests
- error and empty states
- key mutations such as review updates and rule edits

## Out of scope for this completion pass

The following are not required for calling phase 1 and phase 2 complete in this pass:

- ML-based pattern discovery
- AI-authored rule changes
- autonomous trading actions
- a separate journaling application
- a full visual workspace builder for journal screens

## Success criteria

This work is complete only when all of the following are true.

### Backend success criteria

- aggregate summary APIs work for day/week/month/year/since inception
- benchmark comparison works across supported strategy families
- option, paper, algo, and investment flows converge into the same journal contract reliably
- open-run equity handling is materially stronger than the current approximation
- rule/review workflows are fully supported by backend APIs
- operator tools exist for backfill/recompute/freshness/lag visibility

### Frontend success criteria

- Journal appears as a first-class navigation destination in the shared app shell
- `/journal` is dashboard-first and functional
- Overview, Calendar, Trades, Strategies, Rules, and Insights pages exist and are usable
- frontend supports core review, decision-event, and rule-management flows
- UI follows the current operator-shell visual language with dense analytics readability

## Execution order

1. finish backend phase 1 and phase 2 completely
2. verify backend correctness with focused and nearby regressions
3. implement frontend journal workspace routes and components
4. verify frontend routes and interaction tests
5. do a review pass and fix findings

## Implementation note

The implementation plan should be updated rather than replaced entirely, because a large amount of backend foundation work is already present in the working branch.

That updated plan should:

- mark completed backend foundation work as already done
- define the remaining backend-completion tasks explicitly
- define frontend route/component tasks explicitly
- identify which steps can be parallelized safely with subagents/worktrees
