# Trading Journal Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish backend phase 1 and phase 2 for the trading journal, then implement a functional dashboard-first journal workspace in `frontend-next/`.

**Architecture:** Extend the existing Python `journaling/` backend to support aggregate summaries, stronger lifecycle syncing, rules/review workflows, and operator controls. After the backend contract is complete, add a modular Next.js journal workspace under `frontend-next/app/(app)/journal/`, reusing the current app shell/operator components and using UI-builder/ui-ux guidance only for narrow file-targeted UI improvements.

**Tech Stack:** FastAPI, Postgres, SQLAlchemy sessions, existing Python runtime services, Next.js App Router, React, Tailwind, existing operator components, Vitest, pytest.

---

## File structure map

### Backend files to modify

- Modify: `journaling/repository.py` — aggregate queries, rules/review queries, strategy/trade/insight queries, improved benchmark/equity access
- Modify: `journaling/service.py` — aggregate summary orchestration, calendar/trades/strategies/rules/insights APIs, lifecycle sync helpers
- Modify: `journaling/runtime.py` — richer refresh orchestration and safe background behavior
- Modify: `api/routers/journal.py` — new summary/calendar/trades/strategies/rules/insights/review endpoints
- Modify: `strategies/option_strategy/store.py` — stronger journal status/result sync
- Modify: `paper_runtime/service.py` — complete journal attribution path for order/trade updates
- Modify: `algo_runtime/kernel.py` — preserve non-blocking decision capture and broaden safe journal linkage
- Modify: `strategies/momentum.py` — investment cycle/rebalance linkage shape
- Modify: `scripts/backfill_trading_journal.py` — fuller backfill/reporting
- Modify: `scripts/recompute_journal_metrics.py` — richer recompute/admin output
- Modify: `documents/kite-backend-progress.md`
- Modify: `features-doc/trading-journal/progress.md`
- Modify: `features-doc/trading-journal/testing.md`

### Backend tests to add or extend

- Modify: `tests/journaling/test_service.py`
- Modify: `tests/test_journal_router.py`
- Modify: `tests/test_journal_runtime.py`
- Modify: `tests/test_journal_source_hooks.py`
- Create: `tests/journaling/test_aggregate_summaries.py`

### Frontend files to create

- Create: `frontend-next/app/(app)/journal/page.tsx`
- Create: `frontend-next/app/(app)/journal/calendar/page.tsx`
- Create: `frontend-next/app/(app)/journal/trades/page.tsx`
- Create: `frontend-next/app/(app)/journal/strategies/page.tsx`
- Create: `frontend-next/app/(app)/journal/rules/page.tsx`
- Create: `frontend-next/app/(app)/journal/insights/page.tsx`
- Create: `frontend-next/lib/journal/types.ts`
- Create: `frontend-next/lib/journal/api.ts`
- Create: `frontend-next/components/journal/journal-header.tsx`
- Create: `frontend-next/components/journal/journal-nav.tsx`
- Create: `frontend-next/components/journal/overview-kpis.tsx`
- Create: `frontend-next/components/journal/benchmark-comparison-card.tsx`
- Create: `frontend-next/components/journal/review-queue-card.tsx`
- Create: `frontend-next/components/journal/recent-runs-card.tsx`
- Create: `frontend-next/components/journal/calendar-heatmap.tsx`
- Create: `frontend-next/components/journal/trades-table.tsx`
- Create: `frontend-next/components/journal/strategy-performance-table.tsx`
- Create: `frontend-next/components/journal/rules-panel.tsx`
- Create: `frontend-next/components/journal/insights-feed.tsx`
- Create: `frontend-next/components/journal/review-drawer.tsx`
- Create: `frontend-next/components/journal/rule-editor-dialog.tsx`

### Frontend files to modify

- Modify: `frontend-next/lib/navigation.ts`
- Modify: `frontend-next/tests/secondary-pages.test.tsx` or create targeted journal tests
- Create: `frontend-next/tests/journal-page.test.tsx`
- Create: `frontend-next/tests/journal-subpages.test.tsx`

---

### Task 1: Complete aggregate summary backend contract

**Files:**
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Modify: `api/routers/journal.py`
- Create: `tests/journaling/test_aggregate_summaries.py`

- [ ] Add repository queries/helpers for day/week/month/year/since-inception aggregate windows.
- [ ] Add service methods that compute and return aggregate summary payloads by all strategies, strategy family, strategy name, execution mode, and benchmark.
- [ ] Add router endpoints for summary and calendar-level summary access.
- [ ] Add focused tests for summary windows, benchmark/excess return presence, and safe null metrics when data is insufficient.

### Task 2: Strengthen equity, open-run, and benchmark correctness

**Files:**
- Modify: `journaling/service.py`
- Modify: `journaling/repository.py`
- Modify: `tests/journaling/test_service.py`

- [ ] Improve equity-point rebuilding so open runs can include better mark-to-market behavior instead of the current narrow approximation.
- [ ] Ensure benchmark alignment is window-consistent and explicit for incomplete coverage.
- [ ] Ensure Sharpe and Sortino remain gated on adequate periodic return data.
- [ ] Extend tests around open-run equity rebuilding and benchmark alignment.

### Task 3: Finish lifecycle sync across options, paper, algo, and investment flows

**Files:**
- Modify: `strategies/option_strategy/store.py`
- Modify: `paper_runtime/service.py`
- Modify: `algo_runtime/kernel.py`
- Modify: `strategies/momentum.py`
- Modify: `journaling/service.py`
- Modify: `tests/test_journal_source_hooks.py`

- [ ] Extend option strategy journal sync beyond create-time mirroring so status/result changes update linked journal runs.
- [ ] Ensure paper order/trade attribution can update linked runs idempotently.
- [ ] Preserve non-breaking algo decision capture while keeping journaling failures isolated from algo execution.
- [ ] Solidify investment cycle/rebalance linkage so summary behavior is strategy-cycle oriented, not holding-row oriented.
- [ ] Add focused tests for status sync and investment-cycle linkage.

### Task 4: Finish rules, review queue, and insights backend flows

**Files:**
- Modify: `journaling/repository.py`
- Modify: `journaling/service.py`
- Modify: `api/routers/journal.py`
- Modify: `tests/journaling/test_service.py`
- Modify: `tests/test_journal_router.py`

- [ ] Add rule CRUD read/write support and rule evidence rollups.
- [ ] Add review queue queries and review-state update support.
- [ ] Add insight-feed generation from stored reviews, decisions, rules, and benchmark results.
- [ ] Add router coverage for rules, insights, and review-queue endpoints.

### Task 5: Finish operator controls and backend docs

**Files:**
- Modify: `scripts/backfill_trading_journal.py`
- Modify: `scripts/recompute_journal_metrics.py`
- Modify: `journaling/runtime.py`
- Modify: `features-doc/trading-journal/progress.md`
- Modify: `features-doc/trading-journal/testing.md`
- Modify: `documents/kite-backend-progress.md`

- [ ] Add clearer dry-run/reporting output for backfill and recompute scripts.
- [ ] Expose benchmark freshness / runtime lag signals through runtime helper or script output.
- [ ] Refresh feature docs and backend tracker to reflect completed backend phase status.

### Task 6: Verify backend completion before frontend

**Files:**
- No new files required

- [ ] Run focused journal/backend tests.
- [ ] Run nearby regression tests that still exist and are relevant.
- [ ] Stop and fix backend gaps before starting frontend if any verification fails.

### Task 7: Add frontend journal data layer and navigation

**Files:**
- Create: `frontend-next/lib/journal/types.ts`
- Create: `frontend-next/lib/journal/api.ts`
- Modify: `frontend-next/lib/navigation.ts`
- Create: `frontend-next/tests/journal-page.test.tsx`

- [ ] Add typed journal API client helpers around the backend routes.
- [ ] Add Journal to the app shell navigation.
- [ ] Add initial tests that assert the Journal workspace can render and navigation includes it.

### Task 8: Build journal overview page and shared components

**Files:**
- Create: `frontend-next/app/(app)/journal/page.tsx`
- Create: `frontend-next/components/journal/journal-header.tsx`
- Create: `frontend-next/components/journal/journal-nav.tsx`
- Create: `frontend-next/components/journal/overview-kpis.tsx`
- Create: `frontend-next/components/journal/benchmark-comparison-card.tsx`
- Create: `frontend-next/components/journal/review-queue-card.tsx`
- Create: `frontend-next/components/journal/recent-runs-card.tsx`
- Create: `frontend-next/components/journal/calendar-heatmap.tsx`
- Modify: `frontend-next/tests/journal-page.test.tsx`

- [ ] Reuse current operator panels/KPI patterns for a dashboard-first journal landing page.
- [ ] Show KPI summary, benchmark comparison, recent runs, review queue, and mini calendar.
- [ ] Use UI-builder only for narrow file-targeted help if needed.
- [ ] Add tests for overview rendering, empty state, and error state.

### Task 9: Build journal subpages

**Files:**
- Create: `frontend-next/app/(app)/journal/calendar/page.tsx`
- Create: `frontend-next/app/(app)/journal/trades/page.tsx`
- Create: `frontend-next/app/(app)/journal/strategies/page.tsx`
- Create: `frontend-next/app/(app)/journal/rules/page.tsx`
- Create: `frontend-next/app/(app)/journal/insights/page.tsx`
- Create: `frontend-next/components/journal/trades-table.tsx`
- Create: `frontend-next/components/journal/strategy-performance-table.tsx`
- Create: `frontend-next/components/journal/rules-panel.tsx`
- Create: `frontend-next/components/journal/insights-feed.tsx`
- Create: `frontend-next/tests/journal-subpages.test.tsx`

- [ ] Calendar page should emphasize day/week/month/year drilldown summaries.
- [ ] Trades page should provide tabular run/trade exploration.
- [ ] Strategies page should compare strategy families and named strategies against Nifty50.
- [ ] Rules page should support rule management and evidence visibility.
- [ ] Insights page should surface review/rule/benchmark-derived narrative insights.

### Task 10: Add frontend review and rule editing flows

**Files:**
- Create: `frontend-next/components/journal/review-drawer.tsx`
- Create: `frontend-next/components/journal/rule-editor-dialog.tsx`
- Modify: `frontend-next/app/(app)/journal/page.tsx`
- Modify: `frontend-next/app/(app)/journal/rules/page.tsx`
- Modify: `frontend-next/tests/journal-page.test.tsx`
- Modify: `frontend-next/tests/journal-subpages.test.tsx`

- [ ] Add a review interaction path for recent runs / review queue.
- [ ] Add a basic rule create/edit flow.
- [ ] Verify success, empty, and error feedback for mutations.

### Task 11: Final verification and review pass

**Files:**
- No new files required

- [ ] Run journal backend tests.
- [ ] Run frontend test suite for journal pages/components.
- [ ] Run a reviewer pass and fix any substantive findings.

---

## Verification commands

- [ ] Run: `python3 -m pytest tests/journaling/test_repository.py tests/journaling/test_metrics.py tests/journaling/test_benchmark.py tests/journaling/test_service.py tests/journaling/test_aggregate_summaries.py tests/test_journal_router.py tests/test_journal_runtime.py -v`
- [ ] Run: `python3 -m unittest tests.test_journal_source_hooks`
- [ ] Run: `python3 -m py_compile journaling/service.py journaling/runtime.py api/routers/journal.py strategies/option_strategy/store.py paper_runtime/service.py algo_runtime/kernel.py strategies/momentum.py main.py`
- [ ] Run: `npm test -- --runInBand` or `npm test` inside `frontend-next/`
- [ ] Run: `npm run typecheck && npm run lint` inside `frontend-next/`

---

Plan complete and saved to `docs/superpowers/plans/2026-04-14-trading-journal-completion.md`.

Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

User explicitly requested execution after updating the plan, so proceed inline in this session using executing-plans.
