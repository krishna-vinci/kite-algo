# Alerts editor: two-zone workspace and navigation — design

**Status:** DESIGN (approved in chat, 2026-09-16). Implementation follows this
document; where source and this document disagree, current source wins and the
document is corrected in the same slice.

**Base:** `development` at or after `76ca985`.

**Scope guard:** the alert authoring page (`/alerts/new`,
`/alerts/[workflowId]/edit`), page headers/back navigation across the alerts
area, and an unsaved-work guard on the editors. Screener authoring is NOT
redesigned here (its layout port, wizard removal, and dry-run preview are a
separate slice); the screener editors only receive the shared header and the
dirty guard. No backend changes, no API changes, no changes to validation,
save, idempotency, or live-stream logic.

---

## 0. What is being changed, and why

The unified alert editor replaced two competing creation paths with one
authoring page, and its logic is sound: live prices, background validation,
plain-language settings, lossless code view. Its **layout** is the problem:

| Problem | Evidence in source |
| --- | --- |
| One serial column; most of a wide viewport is empty | `unified-alert-editor.tsx` stacks five full-width `Panel`s (instrument, rule, frequency, destinations, name); the fixed save bar constrains itself to `max-w-5xl` |
| The page's intelligence is nearly invisible | The preview sentence renders in `text-xs` inside the fixed save bar; the live price is a small strip inside the instrument panel |
| Frequency is three stacked full-width radio cards for a 3-way choice | `FREQUENCY_OPTIONS` block, rule panel pattern |
| No way back from the deepest pages | The new/edit alert and new/edit screener surfaces render no header link; only the detail pages have "← All alerts" |
| Unsaved work evaporates | No dirty-state tracking anywhere in either editor |

The redesign keeps every behaviour and contract of the unified authoring
design (`alerts-unified-authoring-design.md`) and changes only the geometry
and prominence: **form on the left, consequence on the right**.

---

## 1. Two-zone workspace layout

The editor renders as a CSS grid on `lg` and wider:

```
grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]
```

- **Left column** — the form. Everything the operator acts on, compressed.
- **Right column** — the live side panel (§3): everything the system knows
  about the draft being built. `sticky top-4 self-start`, so it stays on
  screen while the form scrolls.
- Below `lg`: single column. The live price line stays inline under the
  instrument picker (as today) for immediacy, and the full side panel renders
  as a normal block after the name section, before the save bar.
- The AppShell `main` imposes no max width (`components/app-shell.tsx`), so no
  shell changes are needed. A `max-w-6xl` cap on the grid keeps the form from
  stretching absurdly on ultrawide monitors.

## 2. Left column: five panels become four compact sections

1. **Instrument.** Unchanged behaviour: `InstrumentPicker` or
   `UniverseTargetingEditor`, with the "Scan a universe instead" toggle. On
   `lg`+ the inline price strip is replaced by the rail's price card; below
   `lg` it stays where it is.
2. **The rule.** One visual row: operator select + target input. Target
   shortcuts (`[Use current price] [+0.5%] …`) stay directly under the input.
   Evaluation and (when `needsTimeframe`) timeframe become a second compact
   row of inline selects with their hints. The "All conditions and groups"
   disclosure is unchanged.
3. **Notifications.** Frequency becomes a **segmented control**: one row, one
   segment per `FREQUENCY_OPTIONS` value, `role="radiogroup"`/`role="radio"`
   preserved for accessibility and existing tests. The selected option's hint
   renders once beneath the control (replacing three permanently visible hint
   lines). "More timing and noise controls" keeps its collapsed `details`
   disclosure with the existing 2-column grid inside. Destinations become
   toggle chips (`Checkbox`-label pairs wrapped inline) instead of a bare
   checkbox list. The delivery-honesty line stays.
4. **Name.** One compact row: label + input with the generated name, as today.
   In edit mode the section order and contents are identical.

Section anchors (`id="section-instrument"`, `-rule`, `-notifications`, `-name`)
exist for the rail's issue links (§3.4).

## 3. Right rail: the live side panel

One component, `LiveSidePanel`, composed of five cards top to bottom:

1. **Price card.** Large tabular-nums price, freshness `StatusBadge`, age, and
   today's change percent. Data source is the existing `useMarketQuote` /
   `useQuotePresentation` pair — no new subscriptions. Multi-instrument and
   universe alerts show coverage text instead of one price, exactly as the
   current editor refuses to pretend one price speaks for a universe.
2. **Price ladder.** A pure presentational `PriceLadder` component: a
   horizontal track with a marker for the current price and a marker for the
   target, the distance rendered as text (`₹1,140 away · 1.3%`). The track
   range is `min/max(price, target) ± 25%` of their span (or ±5% of price when
   only a price exists), clamped so both markers always sit inside the track.
   Null-safety: no price → "waiting for the first price"; no target → track
   with price marker only and a hint to set the level. This is decoration on
   numbers the page already has — it computes nothing authoritative.
3. **Insight card.** The preview sentence and validation state, promoted from
   `text-xs`-in-the-save-bar to the rail's centre at readable size, with the
   same six states the authoring design defines (valid and ready; waiting for
   required input; valid but currently beyond a crossing level; invalid;
   validation service unavailable; stale/no market data). Tone and wording
   come from the existing `useDefinitionValidation` result — no new state.
4. **Issues card.** `validation.bySection` grouped issues; each group heading
   is a link that scrolls the corresponding left-column section into view
   (anchor ids from §2). This replaces the save bar's issue dump.
5. **Summary card.** "You are creating" (create) / "You are editing" (edit):
   the effective name, the rule in one plain-language line (`describeRule`),
   the frequency phrase (`describeFrequency`), and destination names. This is
   the reading of exactly what will be saved, derived from `effectiveDraft` —
   the same object the save path uses, never a parallel interpretation.

## 4. Save bar

The fixed bottom bar stays (activation must not require scrolling). It is
decluttered to: `Save draft`, `Create and activate` / `Save changes`,
`Save and activate latest`, and on the right the validation `StatusBadge` plus
the preview sentence's short form. Detailed issue lists live in the rail now;
the bar keeps at most the validation-state line and the validation-service
error. All save semantics — idempotency key reuse, 409 recovery with reload
choice, partial-success ("draft was saved, activation failed") messaging — are
byte-for-byte unchanged.

## 5. Navigation: one header pattern for the alerts area

A shared `AlertsPageHeader` component:

```
[← Alerts]   Alerts / Screeners / {name} / Edit          [Form | Code view]
```

- The leading chevron is a `Link` back to the logical parent: the list page
  for top-level children, the detail page for an edit surface.
- The trail is plain text except `{name}`, which links to the detail page when
  the editor is in edit mode.
- A right-hand slot carries page-specific controls (the Form/Code toggle on
  the alert editor).

Mounted on:

| Surface | Trail |
| --- | --- |
| `/alerts/new` | `Alerts / New alert` |
| `/alerts/[id]/edit` | `Alerts / {name} / Edit` |
| `/alerts/screeners/new` (quick + advanced) | `Alerts / Screeners / New screener` |
| `/alerts/screeners/[id]/edit` (form + advanced) | `Alerts / Screeners / {name} / Edit` |
| Detail pages (workflow, screener, universe) | existing "← All alerts" link replaced by the same header, trail `Alerts / {name}` etc. |

The ad-hoc "Back to the quick form" link on `/alerts/screeners/new?mode=advanced`
and the note-link on the screener edit page keep working but sit inside the
page, under the shared header.

## 6. Unsaved-work guard

A `useDirtyGuard` hook, used by the alert editor (form and code views) and
both screener editors:

- **Dirty test:** a stable serialization of the effective draft vs. the draft
  as loaded (for edits, against the loaded document's draft, not the server's
  raw document). The code view's text state participates when it is the active
  view.
- **When dirty:** a `beforeunload` listener covers tab close and refresh.
- **Controlled exits** (the header's back link, the quick↔advanced switch):
  intercepted with a confirmation dialog — *Leave without saving / Keep
  editing* — before navigating.
- **Known limitation, stated in code:** Next.js App Router exposes no
  navigation-interruption API, so browser back/forward and AppShell sidebar
  clicks are not interceptable; `beforeunload` still covers refresh/close,
  which is the dominant loss path. A router-level interception is a possible
  follow-up if it proves necessary, not part of this slice.

## 7. Component and file plan

New:

- `features/alerts/components/alerts-page-header.tsx` — breadcrumb header.
- `features/alerts/components/live-side-panel.tsx` — rail (§3), receiving the
  quote presentation, validation result, effective draft, and mode.
- `features/alerts/components/price-ladder.tsx` — pure visual, no hooks.
- `features/alerts/lib/use-dirty-guard.ts` — hook (§6).

Modified:

- `unified-alert-editor.tsx` — restructure to the two-zone grid; the rule,
  frequency, destinations, and name sections compact per §2; mount the header,
  rail, and guard. Save/validation/stream logic untouched.
- `screener-editor.tsx`, `quick-screener-composer.tsx` — shared header + dirty
  guard only.
- `alerts-new-page.tsx` and the screener new/edit route files — replace ad-hoc
  back links with the shared header where they currently render their own.
- `workflow-detail-page.tsx`, `screener-page.tsx`, `universe-detail-page.tsx` —
  swap the "← All alerts" ghost button for the shared header.

## 8. Testing

- Existing `unified-alert-editor.test.tsx` keeps passing, updated only where
  the DOM legitimately changed (frequency radio cards → segmented control with
  the same roles; issue list moved from save bar to rail). No test is weakened
  or deleted to make the redesign pass.
- New unit tests: `PriceLadder` (marker positions within range, clamping,
  price-only and no-target null paths), `AlertsPageHeader` (trail, back href,
  right slot), `useDirtyGuard` (clean → no listener; dirty → `beforeunload`
  registered, controlled exit opens the dialog; leaving fires the callback).
- One integration assertion: the rail renders the preview sentence when
  validation provides one, and coverage text instead of a price for universe
  alerts.

## 9. Preserved contracts

Everything in `alerts-unified-authoring-design.md` §8 stands: server
authoritative capabilities and validation; catalog-backed identity;
`expected_revision` concurrency; no-op edit hash preservation; 409 recovery
without input loss; idempotency per creation attempt; side-effect-free preview
with its debounce/cancellation; activation guard and silence contract;
lossless Code view (unmodeled constructs route to it, structured saves merge
onto the stored document); live-price freshness vocabulary; no order
capability anywhere. The plain-language settings table is unchanged — the
redesign re-renders the same words in better geometry.

## 10. Non-goals

Screener authoring redesign (single-page authoring, dry-run preview, plain
language for rank/attachment vocabulary), sparkline/candle history in the rail
(needs a backend endpoint; the ladder is the default until then), list-page
changes, bulk actions, template galleries, router-level navigation
interception, and any backend or API change.
