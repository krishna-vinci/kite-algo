# Frontend Next Foundation Design

Date: 2026-04-08
Status: Reference draft, revised after UI lock-in review

## Objective

Create a new frontend foundation in `frontend-next/` for Kite Algo using Next.js and shadcn/ui, while leaving the current Svelte frontend untouched.

This document is a **reference draft**, not a final product-definition lock.

Locked from current work:

- the core visual design system
- the shared app shell direction
- the current options workspace interaction patterns that were explicitly approved

Still not fully locked:

- full product information architecture
- exact module priorities beyond current mockups
- non-options workflows in full detail
- charting experience details
- custom display/workspace composition model

## Why this change is needed

The current repo has an active Svelte frontend in `frontend/`, but the next phase of product work needs a new UI foundation with:

- stronger composability
- better long-term ergonomics for data-heavy operator screens
- a modern component system around shadcn/ui
- cleaner state boundaries for runtime, paper trading, alerts, charts, custom display, and screening workbenches

The goal is not an immediate frontend rewrite. The goal is to establish a parallel Next.js app that can grow incrementally and eventually replace or outpace the current Svelte app.

## Scope status

### Locked in this draft

- new app lives in `frontend-next/`
- use Next.js App Router + TypeScript
- use shadcn/ui + Tailwind CSS
- use React Query, Zustand, RHF, Zod, TanStack Table, lightweight-charts
- dark terminal-style operator UI direction
- shared app shell pattern
- options workspace UI direction and key approved interactions

### Still open / reference only

- final overall product stance beyond current draft pages
- final nav/module order for long-term product evolution
- exact first implementation sequence after scaffolding
- full strategy/investing/F&O/non-options workflow depth
- charts module depth and behaviors
- custom display/dashboard builder capabilities

## Repo placement decision

The new app will live at:

- `frontend-next/`

This remains the lowest-risk path because:

- it does not disrupt the existing Svelte app in `frontend/`
- it avoids immediate deployment churn beyond adding a parallel app
- it keeps migration incremental and reversible

## Product stance

This should no longer be treated as an **options-first product lock**.

Current reference stance:

**Kite Algo is a modular operator-facing trading frontend.**

Options workflows are currently the most detailed and best-resolved draft area, but the product itself must remain broader than options alone.

The draft must stay compatible with:

- trading workflows
- options/F&O workflows
- algo operations
- alerts
- screeners
- paper trading
- charts
- custom displays
- future investing or monitoring workbenches

## Approved stack

### Core framework

- Next.js (App Router)
- TypeScript
- Tailwind CSS
- shadcn/ui

### Server state

- `@tanstack/react-query`

Use React Query for backend-backed state, caching, refetching, invalidation, polling, and mutation lifecycles.

### Client state

- `zustand`

Use Zustand only for local interactive state such as:

- quick-order drafts
- option-chain UI state
- screener builder local state
- chart display state
- custom layout/display state
- panel/dialog/sheet visibility where colocated component state is not enough

Avoid using Zustand as a duplicate server-state cache.

### Forms and validation

- `react-hook-form`
- `zod`
- `@hookform/resolvers`

### UI and utility libraries

- `lucide-react`
- `sonner`
- `cmdk`
- `clsx`
- `tailwind-merge`
- `date-fns`

### Data-dense UI

- `@tanstack/react-table`

### Charting

- `lightweight-charts`

This remains approved, but the exact chart product experience is still open.

### Not included in the foundation yet

These can be added later when a concrete need appears:

- URL-state helpers such as `nuqs`
- virtualization libraries
- heavier charting stacks
- drag/drop systems
- API schema/codegen tooling

## Locked UI design direction

The UI direction is now considered the most stable part of this draft.

### Core visual principles

- dark-first visual treatment
- terminal feel
- monospace typography
- compact but readable spacing
- data-dense but breathable screens
- keyboard-friendly interaction patterns
- minimal decorative UI
- calm visual hierarchy with no casino-like stimulation
- risk/status clarity over visual novelty

### Locked design tokens

```css
:root {
  --bg: #0f1117; --bg-soft: #161820; --panel: #1a1d27; --panel-hover: #1f2230;
  --border: #2a2d3a; --border-soft: #232636;
  --text: #e2e4ea; --muted: #8b8fa4; --dim: #5a5e72;
  --accent: #f97316; --accent-soft: rgba(249,115,22,.1); --accent-border: rgba(249,115,22,.3);
  --green: #34d399; --green-soft: rgba(52,211,153,.08);
  --red: #f87171; --red-soft: rgba(248,113,113,.08);
  --blue: #60a5fa; --blue-soft: rgba(96,165,250,.1); --blue-border: rgba(96,165,250,.25);
  --yellow: #fbbf24;
  --font-mono: 'JetBrains Mono','Fira Code','Cascadia Code',monospace;
}
```

### Locked app shell

All current draft pages should be treated as sharing this shell language:

- left rail: 48px icon rail
- top bar: page title, command input, session chips, mode chip, time
- bottom dock: persistent operational dock for positions/orders/fills and P/L where relevant

This shell is locked as the current reference direction, though exact page-by-page use can still evolve.

## Draft page set

The current mockups should be treated as **reference drafts**, not final feature contracts:

- Dashboard
- Options
- Algos
- Alerts
- Screeners
- Paper
- Settings

Also added explicitly because they were missing from the earlier draft:

- Charts
- Custom Display

## Options-specific spec updates

Only the current **UI and options workflow interactions** should be considered locked here.

### Locked options workspace interactions

- strategy template picker auto-populates legs
- strike adjusters use `+ / -` around editable strike value
- lot adjusters use `+ / -` with quick `×2`
- expiry is directly clickable with dropdown on hover/open
- option chain is vertically scrollable with sticky headers
- chain is not artificially limited to a few rows
- delta search allows targeting a delta and highlighting matching rows
- delta-matched rows use distinct highlight treatment
- resizable panels are a valid recurring pattern
- protection defaults auto-arm based on strategy type
- sessions auto-start and are visible in top bar
- bottom dock remains persistent for operational context

### Locked options UX principles

- low-click order creation
- context-aware option building
- fast leg editing
- visible protection state
- clear payoff/risk framing
- chain and builder working together as one surface

### Not yet locked for options

- full module boundaries for all options sub-pages
- exact data model for every strategy builder state
- exact chart integration inside options flows
- final order ticket model for all product surfaces
- full responsive behavior across every options screen

## Charts module (new explicit draft section)

Charts are part of the product draft and should no longer be implicit only.

### Charts goals

- support fast market inspection
- support options/trading decision context
- support strategy monitoring
- support overlaying relevant metadata without clutter

### Expected chart use cases

- index and instrument price charts
- options-focused contextual charts where useful
- strategy monitoring views
- quick jump from alerts, screeners, and options workspace into chart context

### Draft chart requirements

- dark terminal-aligned styling
- compact control bar
- clear timeframe switching
- support overlays/markers for events where needed
- performant rendering for operator-heavy usage
- keyboard-friendly interactions where practical

### Still open for charts

- whether charts are a standalone primary module or also deeply embedded in other modules
- exact indicator set
- annotation tools
- multi-panel chart layouts
- comparison mode
- strategy payoff/chart relationships

## Custom Display module (new explicit draft section)

Custom Display should be recognized as a missing but important area.

This is not locked in behavior yet, but the architecture should leave room for it.

### What Custom Display means in this draft

- user-arranged monitoring views
- modular panels/widgets
- personalized layouts for watching markets, strategies, alerts, charts, or paper accounts

### Draft expectations

- consistent with shared shell and design tokens
- supports dense monitoring setups
- allows user-prioritized information layout
- should not force a complex builder on day 1

### Still open for Custom Display

- whether layout editing is freeform or template-based
- persistence model for layouts
- widget catalog scope
- drag/drop vs simpler layout presets
- sharing/exporting layouts

## App architecture

### Route groups

The app should use route groups that support future growth without overengineering.

Recommended current shape:

- `app/(auth)/...`
- `app/(app)/layout.tsx`
- `app/(app)/dashboard/page.tsx`
- `app/(app)/options/page.tsx`
- `app/(app)/algos/page.tsx`
- `app/(app)/alerts/page.tsx`
- `app/(app)/screeners/page.tsx`
- `app/(app)/paper/page.tsx`
- `app/(app)/charts/page.tsx`
- `app/(app)/custom-display/page.tsx`
- `app/(app)/settings/page.tsx`

Additional routes can be added later as implementation clarifies the product structure.

### Primary navigation

Navigation order should now be treated as flexible.

Current draft pages that likely deserve primary treatment:

- Dashboard
- Options
- Algos
- Alerts
- Screeners
- Paper
- Charts
- Custom Display
- Settings

This is a draft list, not a final locked nav contract.

### Internal source layout

Recommended `src/` shape:

- `src/app/`
- `src/components/`
- `src/features/algos/`
- `src/features/options/`
- `src/features/alerts/`
- `src/features/screeners/`
- `src/features/paper/`
- `src/features/charts/`
- `src/features/custom-display/`
- `src/lib/api/`
- `src/lib/query/`
- `src/lib/auth/`
- `src/lib/utils/`
- `src/lib/formatters/`
- `src/stores/`
- `src/types/`

## State boundaries

State ownership must remain explicit:

- React Query owns backend data lifecycle
- Zustand owns local workbench and cross-component client state
- React Hook Form owns transient form state
- route/search params can own URL-shareable state when later introduced

The frontend should avoid duplicate ownership of the same data across Query and Zustand.

## Backend integration model

The new frontend will initially consume the existing backend APIs rather than force backend redesign.

Most relevant current API surfaces include:

- runtime and paper operator endpoints in `api/routers/auth.py`
- strategy/operator APIs in `strategies/indexstoploss/router.py`
- options APIs in `broker_api/options_router.py`
- orders/positions runtime APIs in `broker_api/kite_orders.py`

The frontend foundation should introduce a typed API client layer that:

- centralizes fetch behavior
- supports credentials/session usage
- standardizes JSON parsing and error handling
- composes cleanly with React Query hooks

The initial approach should stay hand-authored and pragmatic, not codegen-driven.

## UX requirements for operator screens

Every important screen should be designed to handle:

- loading states
- skeletons for dense content
- empty states
- inline error states
- explicit last-updated timestamps where useful
- retry actions for failed fetches/mutations
- responsive behavior without collapsing into unusable mobile layouts

Accessibility baseline:

- proper semantic controls
- keyboard navigation for major actions
- visible focus states
- labelled forms
- dialogs/sheets with predictable focus handling

## First implementation guidance

The first implementation increment for `frontend-next/` should still deliver a usable foundation, not just a blank scaffold.

Recommended early steps:

1. scaffold the Next.js app in `frontend-next/`
2. install and configure the approved base stack
3. initialize shadcn/ui and the locked design token baseline
4. create the shared app shell
5. create route placeholders/pages for the draft modules
6. establish API layer and Query provider patterns
7. choose the first real screen based on implementation readiness, not only prior draft emphasis

The exact first real feature remains open and should be chosen deliberately after planning.

## Risks and guardrails

### Risk: locking product direction too early from options-heavy drafts

Guardrail:

- keep only UI system and explicitly approved options interactions as locked
- treat broader product/module assumptions as revisitable

### Risk: too much abstraction too early

Guardrail:

- start with feature-first structure and simple typed fetch wrappers
- add shared abstractions only after repetition is real

### Risk: duplicated state across Query and Zustand

Guardrail:

- enforce clear ownership boundaries from the beginning

### Risk: migration churn from replacing the Svelte app too soon

Guardrail:

- keep `frontend/` untouched and build in `frontend-next/` incrementally

## Success criteria

This subproject is successful when:

- `frontend-next/` exists as a clean Next.js foundation
- the app shell and route structure are in place
- the agreed stack is installed and wired correctly
- shadcn/ui is configured and reusable
- the codebase has a clear modular frontend structure
- the locked UI direction is reflected consistently
- the options workspace approved interactions are preserved where implemented
- the architecture still leaves room for charts, custom display, and broader product evolution
