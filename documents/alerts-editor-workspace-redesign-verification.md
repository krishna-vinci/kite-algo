# Alerts editor workspace redesign — verification

**Date:** 2026-09-16 · **Branch:** `development` (single local commit, not pushed)
**Implements:** `documents/alerts-editor-workspace-redesign-design.md` via
`documents/alerts-editor-workspace-redesign-plan.md` (Tasks 1–8).

## Automated gates

| Gate | Result |
| --- | --- |
| Full unit suite (`npm test`) | **374 / 376 pass.** The 2 failures (`tests/reference-pages.test.tsx` "primary strategies operator workspace", `tests/secondary-pages.test.tsx` "paper tab") are **pre-existing**: stashing every file of this slice and re-running on the clean tree reproduces both identically. They stem from the earlier operator-workspace revamp (`features/trading/`, untouched here — zero diff). |
| Typecheck (`npm run typecheck`) | Clean. (One transient error found mid-stream — `ladderRange` receiving `number \| null` — was a plan bug, fixed in `price-ladder.tsx` by widening the parameter.) |
| Lint (`npm run lint`) | No errors from this slice. The guard snapshots were rewritten from a write-during-render `useRef` pattern to a `useState`-captured first-render value after lint flagged `Cannot access refs during render` in all three editors. Remaining 3 errors (`components/bottom-dock.tsx`, `components/workspace/workspace-provider.tsx`) are pre-existing in files this slice never touched. |
| Alerts component tests | `unified-alert-editor.test.tsx` 18/18 (17 pre-existing updated only where the DOM legitimately changed, per plan Task 5 Step 4 — single-match queries became count assertions where text now renders in both the rail and inline; the edit-trail assertion got **stronger**), plus new suites: `alerts-page-header` 4/4, `price-ladder` 8/8, `live-side-panel` 5/5, `use-dirty-guard` 2/2. |

## Browser smoke (dev server, working tree, authenticated session)

`/alerts/new` at 1680×950 — **verified rendering**:

- Two-zone workspace: form left, sticky rail right (`LIVE MARKET` with `NO DATA` / waiting state before an instrument is chosen; `WHAT THIS ALERT WILL DO` showing `WAITING FOR THE REQUIRED FIELDS`; `YOU ARE CREATING` with the rule, frequency phrase, and the preselected destination).
- Header: working `‹ Alerts` back affordance, breadcrumb `Alerts › New alert`, Form/Code toggle in the right slot.
- Frequency renders as the segmented one-row control with only the selected option's hint; destinations render as a checked chip; save buttons disabled with the completeness reasons listed in the bar.

Known cosmetic note: the header reads `‹ Alerts  Alerts › New alert` (back label + trail root are both "Alerts", per the design's mockup). If it reads as duplication in practice, dropping the trail root for top-level children is a one-line follow-up.

**Not verified in-browser:** the interactive flow past initial render (instrument search dropdown, target entry → rail updates, dirty-guard dialog). The dev server's market proxy pointed at `::1:8780` (`ECONNREFUSED` — the container stack publishes market-runtime on 18780), which eventually hung all dev routes; this is a dev-environment port mismatch, not application code. The operator will exercise the full flow against the rebuilt container images, where internal networking is correct.

## Deliberate scope keep-outs (unchanged, verified by diff)

Screener authoring redesign (wizard, dry-run), list page, all backend/API/validation/save/stream logic, `documents/hosted-strategies-proposal-draft.md` and `.commandcode/` (operator's own untracked files, excluded from the commit).

## Follow-ups recorded during implementation

- `SECTION_META` lives in `lib/status.ts`; the alert editor no longer imports it (only the rail does) — intentional.
- Task 6 made `SectionLabel`'s `title` optional (backward compatible; all other callers pass it).
- Screener edit route: the structured-form branch gets its header from `ScreenerEditor` itself, so exactly one header renders per branch.
