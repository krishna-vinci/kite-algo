# Journal Frontend V2 Integration Design

Date: 2026-05-01
Status: approved design direction, ready for implementation planning

## Goal

Integrate the existing Journal V2 backend into the frontend as a coherent, usable Journal workspace inside the existing app shell.

This is not a branding exercise and not a new shell project. The job is to make the Journal frontend properly expose the backend's real V2 capabilities so traders and developers can use it as a serious environment-scoped trading journal and review system.

## Current problem

The backend is already feature-rich, but the frontend still behaves like a set of partially connected utility pages.

Current gaps:

- environment selection is page-local instead of shared across Journal
- pages feel disconnected from each other
- Journal navigation still reflects older/non-V2 structure
- note-taking exists but is not fully surfaced as a real workflow
- unresolved queue exists but does not feel operational
- analytics are functional but presented too raw
- episode detail is not yet strong enough as the primary review surface

## Backend capabilities the frontend must align to

The frontend must be designed around the backend that already exists.

### Environment model

Journal data is hard-partitioned by execution environment:

- `live`
- `paper`
- `dry_run_preview`

Every serious Journal V2 read must be environment-scoped.

### Primary review object

The primary review object is the **episode**, not a raw run and not a raw fill.

### Existing backend-supported user-facing capabilities

- environment listing
- episode listing and episode detail
- episode timeline
- markdown notes
- note revisions/history
- unresolved identity/activity queue
- strategy views
- environment summary analytics
- environment strategy analytics
- explicit paper-vs-live comparison
- attachment metadata creation

### Non-negotiable backend rules

1. Never query performance without environment context.
2. Never silently combine paper and live data.
3. Never treat strategy name alone as canonical grouping identity.
4. Episode is the review and analytics unit.
5. Note reads and writes must respect environment and subject boundaries.

## Design principles

1. **Environment first** — Journal must always make current environment clear.
2. **Workspace cohesion** — Journal pages must feel like one subsystem.
3. **Episode-centered review** — episodes and episode detail are the core review path.
4. **Operational clarity** — unresolved queue and missing live validation state must be visible.
5. **Backend fidelity** — frontend should reflect backend concepts directly instead of inventing parallel abstractions.
6. **Progressive depth** — overview for orientation, detail pages for deep work.

## Scope

### In scope

- shared Journal environment state across Journal routes
- V2-aligned Journal navigation
- Journal overview redesign as operational entry page
- episodes list and episode detail strengthening
- analytics page strengthening
- notes page strengthening
- unresolved page strengthening
- strategies page strengthening
- better state handling: loading, error, empty, no-live-environment

### Out of scope

- app-wide shell redesign
- backend schema or API changes unless a frontend blocker is discovered
- attachment upload UX beyond reflecting current support boundaries
- unresolved queue mutation workflows unless backend support already exists
- generic marketing polish unrelated to Journal functionality

## Information architecture

Journal stays inside the existing app shell.

### Primary Journal routes

- `/journal` — operational overview
- `/journal/episodes` — episode ledger
- `/journal/episodes/[episodeId]` — episode review workspace
- `/journal/analytics` — analytics and comparison
- `/journal/notes` — notes archive and note workflow
- `/journal/unresolved` — unresolved queue
- `/journal/strategies` — environment-scoped strategy scorecards

### De-emphasized legacy/non-core routes

These can remain if already present, but should not dominate the primary Journal V2 flow:

- calendar
- trades
- rules
- insights

If retained in nav, they should be visually secondary or grouped separately.

## Shared Journal environment state

### Requirement

Journal must have a shared selected environment across Journal pages.

### Behavior

- selecting an environment on one Journal page updates the current Journal scope
- navigating to another Journal page preserves the current environment
- direct deep links with `environment_id` still work
- if no environment is selected, V2 pages show controlled empty guidance rather than unsafe data fetches

### Preferred mechanism

Create a Journal-scoped client state layer used only within Journal routes.

Suggested shape:

- `selectedEnvironmentId`
- `selectedEnvironment`
- setter for selected environment
- optional hydration from query string on first load
- optional persistence in URL and/or session storage

### URL behavior

- `environment_id` should remain present in deep links where safety matters
- detail routes must preserve `environment_id`
- overview/subpages may use shared state first, but should still tolerate URL-based re-entry

## Route responsibilities

### `/journal` overview

Purpose: the Journal operational entry page.

Must show:

1. selected environment context
2. top environment metrics
3. recent episodes
4. unresolved queue summary
5. strategy/analytics preview
6. visible live-validation status when no live environment exists

#### Overview layout

- top header: Journal title + environment selector + current environment chip
- KPI band:
  - environment/mode
  - closed episodes
  - net P&L
  - win rate
  - charges
- main left column:
  - recent episodes
  - optional latest note/review activity preview if cheap to fetch
- main right column:
  - unresolved queue summary
  - strategy preview or analytics preview
  - quick links into deep pages

#### Empty states

- no environments exist
- environment selected but no episodes yet
- no live environment exists yet

### `/journal/episodes`

Purpose: the main episode ledger.

Must show:

- environment-scoped episode list
- status
- opened/closed timestamps
- execution context reference
- entry to detail page with preserved environment

Should support:

- loading state
- error state
- empty state
- optional filter controls later for status/context

### `/journal/episodes/[episodeId]`

Purpose: the primary review workspace.

Must show:

- environment-safe episode identity and status
- opened/closed timing
- execution context reference
- timeline section
- note section with real load/save behavior
- note empty state when none exists
- path back to environment-scoped episode list

Should be strong enough that a trader can actually use it for post-trade review.

### `/journal/analytics`

Purpose: environment-scoped performance and comparison analysis.

Must show:

- selected environment summary metrics
- strategy scorecards for selected environment
- explicit paper-vs-live comparison flow using:
  - template
  - paper environment
  - live environment

Must never imply combined paper/live totals.

### `/journal/notes`

Purpose: notes archive and note workflow.

Must support:

- environment-scoped note listing
- useful display of note type, title, updated time, and subject linkage
- creating/editing notes in a real scoped flow
- path toward note revisions visibility

This page should feel like a usable archive and writing surface, not a placeholder editor.

### `/journal/unresolved`

Purpose: unresolved identity/activity queue.

Must show:

- reason
- source system
- raw identity summary
- candidate mapping summary if available
- status / created time

Should feel like an operator worklist.

### `/journal/strategies`

Purpose: environment-scoped strategy scorecards.

Must show:

- template display name
- strategy family
- closed episode count
- net P&L
- charges
- win metrics where available

This page is environment-scoped and should not regress into legacy mixed strategy reporting.

## Navigation design

Primary Journal V2 nav should be:

- Overview
- Episodes
- Analytics
- Notes
- Unresolved
- Strategies

Optional secondary grouping for older non-core routes if kept:

- Calendar
- Trades
- Rules
- Insights

Navigation should preserve current Journal environment where appropriate.

## Notes workflow design

Note-taking is a first-class Journal capability.

### Supported backend-aligned behavior

- markdown note body
- note title
- note type
- tags
- subject scoping
- episode-linked notes
- revision history

### Frontend behavior

- episode detail uses note as part of review workflow
- notes archive supports browsing notes across the selected environment
- note editing respects environment and subject boundaries
- if multiple notes exist for a subject later, the UI must not assume only one forever; current implementation can start with a primary/latest note view but should not hardcode a dead-end model

### Revision support

Revision history does not have to be fully elaborate in the first pass, but the design should leave a clear path for:

- “view revisions” from note detail or editor context
- displaying revision count or revision availability

## Data flow

### Shared environment selection

1. load environments
2. resolve selected environment from:
   - URL if present
   - shared Journal state
   - optional remembered last selection
3. page fetches only after environment is known when environment is required

### Episode detail flow

1. require `environment_id`
2. fetch episode detail
3. fetch timeline
4. fetch episode-linked note(s)
5. allow note create/update in place

### Analytics flow

1. selected environment drives summary and strategy analytics
2. explicit paper/live comparison is triggered only after both environment IDs and a template are selected

## Error and empty-state rules

### Must explicitly handle

- no environments available
- selected environment has no episodes
- selected environment has no notes
- selected environment has no unresolved items
- no live environment exists yet
- missing `environment_id` for deep detail route
- backend fetch failure

### Tone

Messages should be operational and direct, not toy-like and not marketing copy.

## Visual/interaction level

The Journal should feel like part of a professional trading platform.

That means:

- consistent spacing and panel rhythm
- strong section hierarchy
- dense but readable information surfaces
- restrained status/color usage
- practical empty states
- no “demo shell” feel

The goal is usability and coherence, not decorative redesign.

## Accessibility and responsiveness

- environment selector must stay usable on laptop widths
- nav should remain horizontally scrollable if needed
- cards/tables should collapse cleanly on smaller widths
- note editor and timeline should remain readable without requiring perfect widescreen layouts

## Testing requirements

### Frontend tests to maintain/update

- Journal page rendering tests
- episodes/detail tests
- analytics tests
- environment-scoping navigation tests
- note editor behavior tests where touched

### Verification commands

- `cd frontend-next && npm run typecheck`
- `cd frontend-next && npm test -- journal-v2-api.test.ts journal-v2-pages.test.tsx`

## Implementation guidance

Implementation should favor small, reviewable increments:

1. shared environment state + nav alignment
2. overview integration cleanup
3. episodes/detail strengthening
4. analytics/strategies strengthening
5. notes/unresolved strengthening

## Risks

- over-polishing visuals without fixing workflow cohesion
- regressing environment-safety during shared-state introduction
- allowing links/pages to silently drop environment scope
- assuming too simple a note model when revisions and multiple notes may matter

## Success criteria

The frontend work is successful when:

1. Journal behaves like one subsystem rather than disconnected pages.
2. Environment scope is shared and hard to lose accidentally.
3. Episodes, notes, unresolved queue, strategies, and analytics are all usable from the frontend.
4. Episode detail is a credible review workspace.
5. The UI feels aligned to the backend’s real V2 model, not a thin placeholder over APIs.
