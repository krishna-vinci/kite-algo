# Alerts Editor Workspace Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the alert authoring page from a serial single-column form into a two-zone workspace (form on the left, live consequence panel on the right), and give every alerts sub-page a shared header with a working way back plus an unsaved-work guard.

**Architecture:** Four new standalone units (`AlertsPageHeader`, `PriceLadder`, `LiveSidePanel`, `useDirtyGuard`), then a restructure of `UnifiedAlertEditor` that wires them into a `lg:grid-cols-[minmax(0,1fr)_22rem]` layout, then a navigation rollout to the screener surfaces and detail pages. All save/validation/live-stream logic moves verbatim — nothing in this plan changes a contract.

**Tech Stack:** Next.js App Router, React 19, TanStack Query, Tailwind, shadcn/ui primitives (`components/ui/*`), vitest + Testing Library.

**Spec:** `documents/alerts-editor-workspace-redesign-design.md` — read it first; this plan argues from it.

## Global Constraints

- Work on `development`. **Commits are local only — NEVER push** (explicit user instruction).
- Interactive GPG signing is unavailable in this environment: every `git commit` in this plan uses `--no-gpg-sign`. The user may re-sign later.
- All commands run from `/home/krishna/kite-algo/frontend-next` unless stated otherwise.
- Tests: `npx vitest run <path>` for a file, `npm test` for the suite. Typecheck: `npm run typecheck`.
- No backend, API, validation, idempotency, 409-recovery, preview-debounce, or market-stream changes. The save mutation, `useDefinitionValidation` usage, `useMarketQuote` usage, and `buildDocument` calls move verbatim.
- Plain-language copy is unchanged: keep exact strings `When should we notify you?`, `Notify via`, `Save draft`, `Create and activate`, `Save changes`, `Save and activate latest`, aria-labels `Condition`, `Target value`, `Evaluation`, `Measured over`, and the `role="radiogroup"`/`role="radio"` frequency semantics (existing tests query them).
- Tests are never weakened or deleted; they are updated only where the DOM legitimately changed, as enumerated in Task 5.
- YAGNI: no sparkline, no bulk actions, no screener authoring redesign (separate slice).

## File Structure

| File | Responsibility |
| --- | --- |
| `features/alerts/components/alerts-page-header.tsx` | Create. Shared breadcrumb header with back link, trail, right slot. |
| `features/alerts/components/price-ladder.tsx` | Create. Pure price-vs-target track; exports `ladderRange`. |
| `features/alerts/lib/status.ts` | Modify. Gains shared presentation maps `VALIDATION_TONE`, `VALIDATION_LABEL`, `SECTION_META` (moved out of the editor to avoid a cycle). |
| `features/alerts/components/live-side-panel.tsx` | Create. Right rail: price card, ladder, insight, issues, summary. |
| `features/alerts/lib/use-dirty-guard.ts` | Create. `beforeunload` + controlled-exit confirm dialog. |
| `features/alerts/components/unified-alert-editor.tsx` | Modify. Two-zone restructure; header + rail + guard wired in. |
| `features/alerts/components/screener-editor.tsx`, `quick-screener-composer.tsx` | Modify. Header + guard only. |
| `app/(app)/alerts/screeners/new/page.tsx`, `app/(app)/alerts/screeners/[workflowId]/edit/page.tsx` | Modify. Header on the advanced/code branches. |
| `features/alerts/components/workflow-detail-page.tsx`, `screener-page.tsx`, `universe-detail-page.tsx` | Modify. "← All alerts" ghost button → shared header. |
| `features/alerts/components/*.test.tsx` (new + existing) | One test file per new unit; updates to `unified-alert-editor.test.tsx` per Task 5. |

---

### Task 1: AlertsPageHeader

**Files:**
- Create: `features/alerts/components/alerts-page-header.tsx`
- Test: `features/alerts/components/alerts-page-header.test.tsx`

**Interfaces:**
- Consumes: nothing (Leaflink + lucide icons only).
- Produces:
  ```ts
  export type BreadcrumbItem = { label: string; href?: string };
  export function AlertsPageHeader(props: {
    trail: BreadcrumbItem[];       // trail[0] is the area root ("Alerts")
    backHref: string;              // where the chevron goes
    right?: ReactNode;             // page-specific controls (Form/Code toggle)
    onBack?: (href: string) => void; // when set, the chevron calls this instead of navigating (dirty guard)
  }): JSX.Element
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// features/alerts/components/alerts-page-header.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AlertsPageHeader } from "./alerts-page-header";

describe("AlertsPageHeader", () => {
  it("renders the trail, linking intermediate items, marking the last as current", () => {
    render(
      <AlertsPageHeader
        backHref="/alerts"
        trail={[
          { label: "Alerts", href: "/alerts" },
          { label: "Screeners", href: "/alerts/screeners" },
          { label: "momentum-scan", href: "/alerts/screeners/x" },
          { label: "Edit" },
        ]}
      />,
    );
    expect(screen.getByText("momentum-scan").getAttribute("href")).toBe("/alerts/screeners/x");
    const current = screen.getByText("Edit");
    expect(current.getAttribute("href")).toBeNull();
    expect(current.getAttribute("aria-current")).toBe("page");
  });

  it("offers the way back, labelled with the first trail item", () => {
    render(
      <AlertsPageHeader backHref="/alerts" trail={[{ label: "Alerts" }, { label: "New alert" }]} />,
    );
    expect(screen.getByText("Alerts").closest("a")?.getAttribute("href")).toBe("/alerts");
  });

  it("calls onBack instead of navigating when the guard is wired", () => {
    const onBack = vi.fn();
    render(
      <AlertsPageHeader backHref="/alerts" trail={[{ label: "Alerts" }]} onBack={onBack} />,
    );
    fireEvent.click(screen.getByText("Alerts"));
    expect(onBack).toHaveBeenCalledWith("/alerts");
    expect(screen.getByText("Alerts").closest("a")).toBeNull();
  });

  it("renders the right-hand slot", () => {
    render(
      <AlertsPageHeader
        backHref="/alerts"
        trail={[{ label: "Alerts" }]}
        right={<button type="button">Code view</button>}
      />,
    );
    expect(screen.getByText("Code view")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run features/alerts/components/alerts-page-header.test.tsx`
Expected: FAIL — cannot resolve `./alerts-page-header`.

- [ ] **Step 3: Write the implementation**

```tsx
// features/alerts/components/alerts-page-header.tsx
"use client";

/**
 * The one header pattern for the alerts area: a working way back, the trail
 * naming where you are, and a right-hand slot for page-specific controls.
 * `onBack` lets a page route the chevron through its dirty guard instead of
 * navigating directly.
 */

import Link from "next/link";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import type { ReactNode } from "react";

export type BreadcrumbItem = { label: string; href?: string };

export function AlertsPageHeader({
  trail,
  backHref,
  right,
  onBack,
}: {
  trail: BreadcrumbItem[];
  backHref: string;
  right?: ReactNode;
  onBack?: (href: string) => void;
}) {
  const backContent = (
    <>
      <ChevronLeftIcon className="size-4" aria-hidden />
      {trail[0]?.label ?? "Back"}
    </>
  );
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
        {onBack ? (
          <button
            type="button"
            onClick={() => onBack(backHref)}
            className="flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            {backContent}
          </button>
        ) : (
          <Link
            href={backHref}
            className="flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground"
          >
            {backContent}
          </Link>
        )}
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1">
          {trail.map((item, index) => (
            <span key={`${item.label}-${index}`} className="flex min-w-0 items-center gap-1">
              <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground/50" aria-hidden />
              {item.href && index < trail.length - 1 ? (
                <Link
                  href={item.href}
                  className="truncate text-muted-foreground hover:text-foreground"
                >
                  {item.label}
                </Link>
              ) : (
                <span
                  aria-current={index === trail.length - 1 ? "page" : undefined}
                  className="truncate font-medium"
                >
                  {item.label}
                </span>
              )}
            </span>
          ))}
        </nav>
      </div>
      {right ? <div className="flex shrink-0 items-center gap-2">{right}</div> : null}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run features/alerts/components/alerts-page-header.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add features/alerts/components/alerts-page-header.tsx features/alerts/components/alerts-page-header.test.tsx
git commit --no-gpg-sign -m "feat(alerts): shared breadcrumb header for the alerts area"
```

---

### Task 2: PriceLadder

**Files:**
- Create: `features/alerts/components/price-ladder.tsx`
- Test: `features/alerts/components/price-ladder.test.tsx`

**Interfaces:**
- Consumes: `formatPercent`, `formatPrice`, `TargetDistance` from `@/features/alerts/lib/plain-language`.
- Produces:
  ```ts
  export type LadderRange = { min: number; max: number };
  export function ladderRange(price: number, target: number | null): LadderRange | null;
  export function PriceLadder(props: {
    price: number | null;
    target: number | null;
    distance: TargetDistance | null; // describeTarget() result
  }): JSX.Element
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// features/alerts/components/price-ladder.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ladderRange, PriceLadder } from "./price-ladder";

describe("ladderRange", () => {
  it("pads both sides of the price-target span", () => {
    const range = ladderRange(100, 110);
    expect(range).toEqual({ min: 97.5, max: 112.5 });
  });

  it("is symmetric regardless of which value is higher", () => {
    expect(ladderRange(110, 100)).toEqual({ min: 97.5, max: 112.5 });
  });

  it("keeps a visible window when price and target nearly touch", () => {
    const range = ladderRange(100, 100.0001)!;
    expect(range.min).toBeLessThan(100);
    expect(range.max).toBeGreaterThan(100.0001);
  });

  it("with no target, shows a small band around the price", () => {
    expect(ladderRange(100, null)).toEqual({ min: 95, max: 105 });
  });

  it("returns null without a usable price", () => {
    expect(ladderRange(null as unknown as number, 100)).toBeNull();
    expect(ladderRange(Number.NaN, 100)).toBeNull();
  });
});

function markerPct(testId: string): number {
  const el = screen.getByTestId(testId);
  return parseFloat((el as HTMLElement).style.left);
}

describe("PriceLadder", () => {
  it("places the target marker beyond the price marker, both inside the track", () => {
    render(
      <PriceLadder
        price={100}
        target={110}
        distance={{
          distance: 10,
          percent: 10,
          direction: "above",
          sentence: "Target is ₹10.00 above the current price (+10.00%).",
          alreadyBeyond: false,
        }}
      />,
    );
    const pricePct = markerPct("ladder-price-marker");
    const targetPct = markerPct("ladder-target-marker");
    expect(pricePct).toBeGreaterThan(0);
    expect(pricePct).toBeLessThan(100);
    expect(targetPct).toBeGreaterThan(pricePct);
    expect(targetPct).toBeLessThan(100);
    expect(screen.getByText(/₹10\.00 above the current price/)).toBeTruthy();
  });

  it("shows a price-only track and a set-the-level hint without a target", () => {
    render(<PriceLadder price={100} target={null} distance={null} />);
    expect(screen.getByTestId("ladder-price-marker")).toBeTruthy();
    expect(screen.queryByTestId("ladder-target-marker")).toBeNull();
    expect(screen.getByText(/set a level/i)).toBeTruthy();
  });

  it("renders nothing but the waiting hint without a price", () => {
    render(<PriceLadder price={null} target={100} distance={null} />);
    expect(screen.queryByTestId("ladder-price-marker")).toBeNull();
    expect(screen.getByText(/waiting for the first price/i)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run features/alerts/components/price-ladder.test.tsx`
Expected: FAIL — cannot resolve `./price-ladder`.

- [ ] **Step 3: Write the implementation**

```tsx
// features/alerts/components/price-ladder.tsx
/**
 * The price-vs-target track: pure decoration on numbers the page already has.
 * It computes nothing authoritative — the markers are positioned inside a
 * padded window that always contains both values.
 */

import {
  formatPrice,
  type TargetDistance,
} from "@/features/alerts/lib/plain-language";

export type LadderRange = { min: number; max: number };

export function ladderRange(price: number, target: number | null): LadderRange | null {
  if (price === null || !Number.isFinite(price)) return null;
  if (target === null || !Number.isFinite(target)) {
    return { min: price * 0.95, max: price * 1.05 };
  }
  const span = Math.abs(target - price);
  const pad = Math.max(span * 0.25, Math.abs(price) * 0.01);
  return { min: Math.min(price, target) - pad, max: Math.max(price, target) + pad };
}

function positionOf(value: number, range: LadderRange): number {
  const pct = ((value - range.min) / (range.max - range.min)) * 100;
  return Math.min(100, Math.max(0, pct));
}

export function PriceLadder({
  price,
  target,
  distance,
}: {
  price: number | null;
  target: number | null;
  distance: TargetDistance | null;
}) {
  const range = ladderRange(price, target);
  if (!range) {
    return <p className="text-xs text-muted-foreground">Waiting for the first price…</p>;
  }
  return (
    <div className="flex flex-col gap-1.5">
      <div className="relative h-2 rounded-full bg-border/60" role="presentation">
        <span
          data-testid="ladder-price-marker"
          className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
          style={{ left: `${positionOf(price ?? range.min, range)}%` }}
        />
        {target !== null ? (
          <span
            data-testid="ladder-target-marker"
            className="absolute top-1/2 h-4 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded bg-amber-400"
            style={{ left: `${positionOf(target, range)}%` }}
          />
        ) : null}
      </div>
      {distance ? (
        <p className="text-xs text-muted-foreground">{distance.sentence}</p>
      ) : (
        <p className="text-xs text-muted-foreground">Set a level to see the distance.</p>
      )}
      <p className="text-xs tabular-nums text-muted-foreground/70">
        {formatPrice(range.min)} — {formatPrice(range.max)}
      </p>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run features/alerts/components/price-ladder.test.tsx`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add features/alerts/components/price-ladder.tsx features/alerts/components/price-ladder.test.tsx
git commit --no-gpg-sign -m "feat(alerts): price-vs-target ladder for the editor rail"
```

---

### Task 3: Shared presentation maps + LiveSidePanel

**Files:**
- Modify: `features/alerts/lib/status.ts` (append maps)
- Create: `features/alerts/components/live-side-panel.tsx`
- Test: `features/alerts/components/live-side-panel.test.tsx`

**Interfaces:**
- Consumes: `PriceLadder`/`ladderRange` (Task 2), `ValidationResult`/`ValidationState`/`IssueSection` from `@/features/alerts/hooks/use-definition-validation`, `StatusBadge` from `@/components/operator/status-badge`, quote/presentation types from `@/features/alerts/hooks/use-market-stream`.
- Produces (all from `lib/status.ts`):
  ```ts
  export const VALIDATION_TONE: Record<string, "positive" | "warning" | "danger" | "neutral">;
  export const VALIDATION_LABEL: Record<string, string>;
  export const SECTION_META: Array<{ section: IssueSection; label: string; anchor: string }>;
  ```
  and from the component:
  ```ts
  export type PanelSummary = {
    title: string;      // "You are creating" | "You are editing"
    name: string;
    rule: string;       // one-line rule, or coverage text for universes
    frequency: string;  // describeFrequency(effectiveDraft.alert)
    channels: string[]; // selected channel names
  };
  export function LiveSidePanel(props: {
    quote: MarketQuote | null;                    // type from use-market-stream
    presentation: { price: number | null; tone: "positive" | "warning" | "danger" | "neutral"; label: string };
    coverage: string | null;   // universe/multi-instrument text; replaces the price card
    targetValue: number | null;
    distance: TargetDistance | null;
    validation: ValidationResult;
    summary: PanelSummary;
  }): JSX.Element
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// features/alerts/components/live-side-panel.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LiveSidePanel, type PanelSummary } from "./live-side-panel";
import type { ValidationResult } from "@/features/alerts/hooks/use-definition-validation";

const emptyValidation: ValidationResult = {
  state: "ready",
  issues: [],
  bySection: {
    instrument: [],
    rule: [],
    evaluation: [],
    frequency: [],
    destinations: [],
    other: [],
  },
  previewSentence: null,
  preview: null,
  error: null,
  revalidate: () => {},
};

const summary: PanelSummary = {
  title: "You are creating",
  name: "RELIANCE crosses above ₹1,500",
  rule: "RELIANCE crosses above 1500",
  frequency: "Once, then it goes quiet.",
  channels: ["telegram-desk"],
};

describe("LiveSidePanel", () => {
  it("shows the price with freshness, age and receipts", () => {
    render(
      <LiveSidePanel
        quote={{ age_ms: 1000, exchange_timestamp: "2026-09-16T09:15:00Z", received_at: "2026-09-16T09:15:01Z" } as never}
        presentation={{ price: 124860, tone: "positive", label: "LIVE" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("₹1,24,860.00")).toBeTruthy();
    expect(screen.getByText("LIVE")).toBeTruthy();
    expect(screen.getByText(/updated 1s ago/)).toBeTruthy();
    expect(screen.getByText(/exchange .*received/)).toBeTruthy();
  });

  it("shows coverage text instead of one price for universe alerts", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage="50 instruments in NIFTY50"
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("50 instruments in NIFTY50")).toBeTruthy();
    expect(screen.queryByTestId("ladder-price-marker")).toBeNull();
  });

  it("renders the preview sentence and validation state at readable size", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: 100, tone: "positive", label: "LIVE" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={{ ...emptyValidation, state: "ready", previewSentence: "Price is already above the level." }}
        summary={summary}
      />,
    );
    expect(screen.getByText("Valid")).toBeTruthy();
    expect(screen.getByText("Price is already above the level.")).toBeTruthy();
  });

  it("links each issue group to its section anchor", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={{
          ...emptyValidation,
          state: "invalid",
          bySection: { ...emptyValidation.bySection, rule: ["The condition is not valid."] },
        }}
        summary={summary}
      />,
    );
    const link = screen.getByText("Condition").closest("a");
    expect(link?.getAttribute("href")).toBe("#section-rule");
    expect(screen.getByText("The condition is not valid.")).toBeTruthy();
  });

  it("summarises exactly what will be saved", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("You are creating")).toBeTruthy();
    expect(screen.getByText("Once, then it goes quiet.")).toBeTruthy();
    expect(screen.getByText(/telegram-desk/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run features/alerts/components/live-side-panel.test.tsx`
Expected: FAIL — cannot resolve `./live-side-panel`.

- [ ] **Step 3: Append the shared maps to `features/alerts/lib/status.ts`**

These exact objects move OUT of `unified-alert-editor.tsx` (where they live today at lines ~355–372 and ~1025–1032 as locals) so both the editor and the rail can use them without a circular import. Add the needed type imports at the top of `status.ts`:

```ts
import type {
  IssueSection,
  ValidationState,
} from "@/features/alerts/hooks/use-definition-validation";
```

```ts
// -- shared presentation of validation state and issue sections ------------
// Moved out of the editor so the rail and the save bar render one vocabulary.

export const VALIDATION_TONE: Record<string, "positive" | "warning" | "danger" | "neutral"> = {
  ready: "positive",
  crossed: "warning",
  checking: "neutral",
  incomplete: "neutral",
  invalid: "danger",
  unavailable: "warning",
  "no-data": "neutral",
};

export const VALIDATION_LABEL: Record<string, string> = {
  ready: "Valid",
  crossed: "Already past the level",
  checking: "Checking…",
  incomplete: "Waiting for the required fields",
  invalid: "Needs attention",
  unavailable: "Validation unavailable",
  "no-data": "No market data yet",
};

export const SECTION_META: Array<{
  section: IssueSection;
  label: string;
  anchor: string;
}> = [
  { section: "instrument", label: "Instrument", anchor: "section-instrument" },
  { section: "rule", label: "Condition", anchor: "section-rule" },
  { section: "evaluation", label: "Evaluation", anchor: "section-evaluation" },
  { section: "frequency", label: "Notification frequency", anchor: "section-frequency" },
  { section: "destinations", label: "Destinations", anchor: "section-destinations" },
  { section: "other", label: "Definition", anchor: "section-name" },
];
```

(Check first that `status.ts` has no existing export with these names; it does not — it currently holds `deriveLifecycle` only.)

- [ ] **Step 4: Write `LiveSidePanel`**

```tsx
// features/alerts/components/live-side-panel.tsx
"use client";

/**
 * The editor's right rail: everything the system knows about the draft being
 * built, in one sticky column — the live price, where the target sits, what
 * the definition would do right now, what is still wrong (linked to the
 * section that fixes it), and a reading of exactly what will be saved.
 *
 * It renders state the editor already computed; it computes nothing itself.
 */

import Link from "next/link";
import { AlertCircleIcon } from "lucide-react";

import { StatusBadge } from "@/components/operator/status-badge";
import { PriceLadder } from "@/features/alerts/components/price-ladder";
import {
  SECTION_META,
  VALIDATION_LABEL,
  VALIDATION_TONE,
} from "@/features/alerts/lib/status";
import { formatAge, formatPrice, type TargetDistance } from "@/features/alerts/lib/plain-language";
import type { ValidationResult } from "@/features/alerts/hooks/use-definition-validation";

export type PanelSummary = {
  title: string;
  name: string;
  rule: string;
  frequency: string;
  channels: string[];
};

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card/60 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function LiveSidePanel({
  quote,
  presentation,
  coverage,
  targetValue,
  distance,
  validation,
  summary,
}: {
  quote: {
    age_ms: number | null | undefined;
    exchange_timestamp: string | null | undefined;
    received_at: string | null | undefined;
    change_percent: number | null | undefined;
  } | null;
  presentation: { price: number | null; tone: "positive" | "warning" | "danger" | "neutral"; label: string };
  coverage: string | null;
  targetValue: number | null;
  distance: TargetDistance | null;
  validation: ValidationResult;
  summary: PanelSummary;
}) {
  const stateLabel = VALIDATION_LABEL[validation.state] ?? validation.state;
  const stateTone = VALIDATION_TONE[validation.state] ?? "neutral";

  return (
    <div className="flex flex-col gap-4">
      <Card title="Live market">
        {coverage ? (
          <p className="text-sm text-muted-foreground">{coverage}</p>
        ) : (
          <div className="flex flex-col gap-1">
            <span className="flex items-baseline gap-2">
              <span className="text-2xl font-semibold tabular-nums">
                {formatPrice(presentation.price)}
              </span>
              <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
            </span>
            {quote ? (
              <>
                <span className="text-xs text-muted-foreground">{formatAge(quote.age_ms)}</span>
                <span className="text-xs text-muted-foreground">
                  {quote.exchange_timestamp
                    ? `exchange ${new Date(quote.exchange_timestamp).toLocaleTimeString()}`
                    : "no exchange timestamp"}
                  {quote.received_at
                    ? ` · received ${new Date(quote.received_at).toLocaleTimeString()}`
                    : ""}
                </span>
                {quote.change_percent !== null && quote.change_percent !== undefined ? (
                  <span className="text-xs text-muted-foreground">
                    {quote.change_percent >= 0 ? "+" : ""}
                    {quote.change_percent.toFixed(2)}% today
                  </span>
                ) : null}
              </>
            ) : null}
            <PriceLadder price={presentation.price} target={targetValue} distance={distance} />
          </div>
        )}
      </Card>

      <Card title="What this alert will do">
        <span className="flex items-center gap-2">
          <StatusBadge tone={stateTone}>{stateLabel}</StatusBadge>
        </span>
        {validation.previewSentence ? (
          <p className="text-sm">{validation.previewSentence}</p>
        ) : null}
        {validation.error ? (
          <p className="text-xs text-amber-300" role="status">
            {validation.error} Your draft is untouched.
          </p>
        ) : null}
        {SECTION_META.map(({ section, label, anchor }) => {
          const messages = validation.bySection[section] ?? [];
          if (messages.length === 0) return null;
          return (
            <div key={section} className="flex flex-col gap-1">
              <Link href={`#${anchor}`} className="text-xs font-medium underline text-muted-foreground">
                {label}
              </Link>
              <ul className="flex flex-col gap-1 text-xs text-rose-300" role="alert">
                {messages.map((message) => (
                  <li key={message} className="flex items-start gap-1">
                    <AlertCircleIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
                    {message}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </Card>

      <Card title={summary.title}>
        <p className="text-sm font-medium">{summary.name}</p>
        <p className="text-xs text-muted-foreground">{summary.rule}</p>
        <p className="text-xs text-muted-foreground">{summary.frequency}</p>
        <p className="text-xs text-muted-foreground">
          {summary.channels.length > 0
            ? `Via ${summary.channels.join(", ")}`
            : "No destination selected yet."}
        </p>
      </Card>
    </div>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npx vitest run features/alerts/components/live-side-panel.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add features/alerts/lib/status.ts features/alerts/components/live-side-panel.tsx features/alerts/components/live-side-panel.test.tsx
git commit --no-gpg-sign -m "feat(alerts): live side panel with shared validation vocabulary"
```

---

### Task 4: useDirtyGuard

**Files:**
- Create: `features/alerts/lib/use-dirty-guard.ts`
- Test: `features/alerts/lib/use-dirty-guard.test.tsx`

**Interfaces:**
- Consumes: `Dialog`, `DialogContent`, `DialogDescription`, `DialogFooter`, `DialogHeader`, `DialogTitle` from `@/components/ui/dialog`; `Button` from `@/components/ui/button`; `useRouter` from `next/navigation`.
- Produces:
  ```ts
  export function useDirtyGuard(isDirty: boolean): {
    attemptExit: (href: string) => void; // navigates at once when clean, else opens the dialog
    dialog: ReactNode;                   // render next to the page content
  }
  ```

- [ ] **Step 1: Write the failing test**

```tsx
// features/alerts/lib/use-dirty-guard.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import { useDirtyGuard } from "./use-dirty-guard";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

function Harness({ isDirty }: { isDirty: boolean }) {
  const { attemptExit, dialog } = useDirtyGuard(isDirty);
  return (
    <div>
      <button type="button" onClick={() => attemptExit("/alerts")}>
        leave
      </button>
      {dialog}
    </div>
  );
}

describe("useDirtyGuard", () => {
  it("navigates immediately when the draft is clean", () => {
    render(<Harness isDirty={false} />);
    fireEvent.click(screen.getByText("leave"));
    expect(push).toHaveBeenCalledWith("/alerts");
    expect(screen.queryByText("Leave without saving?")).toBeNull();
  });

  it("opens the dialog when dirty; leaving confirms, keeping editing stays", async () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    render(<Harness isDirty={true} />);
    expect(addSpy).toHaveBeenCalledWith("beforeunload", expect.any(Function));

    fireEvent.click(screen.getByText("leave"));
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByText("Leave without saving?")).toBeTruthy();

    fireEvent.click(screen.getByText("Keep editing"));
    expect(push).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("leave"));
    fireEvent.click(screen.getByText("Leave without saving"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/alerts"));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run features/alerts/lib/use-dirty-guard.test.tsx`
Expected: FAIL — cannot resolve `./use-dirty-guard`.

- [ ] **Step 3: Write the implementation**

```ts
// features/alerts/lib/use-dirty-guard.ts
"use client";

/**
 * The unsaved-work guard.
 *
 * Dirty editors register `beforeunload` (tab close, refresh) and route their
 * OWN exit affordances (the header chevron) through `attemptExit`, which asks
 * before leaving. Browser back/forward and AppShell sidebar clicks are not
 * interceptable in the App Router — that limitation is accepted in the design;
 * refresh/close, the dominant loss path, is covered.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function useDirtyGuard(isDirty: boolean): { attemptExit: (href: string) => void; dialog: ReactNode } {
  const router = useRouter();
  const [pendingHref, setPendingHref] = useState<string | null>(null);

  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  const attemptExit = useCallback(
    (href: string) => {
      if (!isDirty) {
        router.push(href);
        return;
      }
      setPendingHref(href);
    },
    [isDirty, router],
  );

  const confirmExit = useCallback(() => {
    const href = pendingHref;
    setPendingHref(null);
    if (href) router.push(href);
  }, [pendingHref, router]);

  const cancelExit = useCallback(() => setPendingHref(null), []);

  const dialog = (
    <Dialog
      open={pendingHref !== null}
      onOpenChange={(open) => {
        if (!open) cancelExit();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Leave without saving?</DialogTitle>
          <DialogDescription>
            Your changes to this draft have not been saved. Leaving now discards them.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={cancelExit}>
            Keep editing
          </Button>
          <Button type="button" variant="destructive" onClick={confirmExit}>
            Leave without saving
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { attemptExit, dialog };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run features/alerts/lib/use-dirty-guard.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add features/alerts/lib/use-dirty-guard.ts features/alerts/lib/use-dirty-guard.test.tsx
git commit --no-gpg-sign -m "feat(alerts): unsaved-work guard for the editors"
```

---

### Task 5: Restructure UnifiedAlertEditor into the two-zone workspace

The largest task. **All logic between the layout anchors is moved verbatim**: the save mutation, `reloadLatest`, completeness `issues`, `validation` wiring, outcome alerts, universe probe, inference, `CodeViewCreate`, `SectionIssues`, the conditions disclosure, the "More timing and noise controls" grid, and the save bar's buttons.

**Files:**
- Modify: `features/alerts/components/unified-alert-editor.tsx`
- Modify: `features/alerts/components/unified-alert-editor.test.tsx` (only the updates enumerated below)

**Interfaces:**
- Consumes: `AlertsPageHeader` (Task 1), `LiveSidePanel` + `PanelSummary` (Task 3), `VALIDATION_TONE`/`VALIDATION_LABEL`/`SECTION_META` from `lib/status` (Task 3), `useDirtyGuard` (Task 4), `Checkbox` from `@/components/ui/checkbox`, `cn` from `@/lib/utils`.
- Produces: the same component signature (`{ scope, mode, initialDraft, conversionError? }`) — no caller changes.

- [ ] **Step 1: Write the failing layout test (append to the existing test file)**

```tsx
// appended to features/alerts/components/unified-alert-editor.test.tsx
it("renders the workspace: sticky rail with summary, and a compact frequency segmented control", async () => {
  renderEditor(); // the file's existing render helper with mocked queries/streams
  expect(await screen.findByText("New alert")).toBeTruthy();
  // The rail exists with its three cards:
  expect(screen.getByText("Live market")).toBeTruthy();
  expect(screen.getByText("What this alert will do")).toBeTruthy();
  expect(screen.getByText("You are creating")).toBeTruthy();
  // Frequency is a segmented radiogroup, one row, hints collapsed to the selected one:
  const group = screen.getByRole("radiogroup", { name: "Notification frequency" });
  expect(group.querySelectorAll("input[type=radio]").length).toBe(3);
  expect(screen.getAllByRole("radio", { name: "Once when it happens" }).length).toBeGreaterThan(0);
});
```

(If the file's existing helper renders `AlertsMarketStreamProvider`, reuse it verbatim; the three rail cards render without market data because every card degrades gracefully.)

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run features/alerts/components/unified-alert-editor.test.tsx`
Expected: FAIL — "Live market" not found (rail does not exist yet).

- [ ] **Step 3: Restructure the component**

Apply these changes to `unified-alert-editor.tsx`, in order:

**(a) Imports.** Add:

```tsx
import { AlertsPageHeader } from "@/features/alerts/components/alerts-page-header";
import { LiveSidePanel } from "@/features/alerts/components/live-side-panel";
import { useDirtyGuard } from "@/features/alerts/lib/use-dirty-guard";
import {
  SECTION_META,
  VALIDATION_LABEL,
  VALIDATION_TONE,
} from "@/features/alerts/lib/status";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";
```

**(b) Delete the now-shared locals** inside the component and at file bottom: `validationTone` (~line 355), `validationLabel` (~line 364), `SECTION_LABELS` (~line 1025). Replace their uses with `VALIDATION_TONE`/`VALIDATION_LABEL`; `sectionsWithIssues()` in the save bar becomes a loop over `SECTION_META`. Keep `SectionIssues` (inline per-section errors stay).

**(c) Dirty snapshot.** After `effectiveDraft` is computed (~line 245), add:

```tsx
// The unsaved-work guard compares the draft as first loaded with what the
// operator sees now; the target text and touched flags are part of the draft's
// story even though they live outside `AlertDraft`.
const dirtySnapshot = JSON.stringify({ draft: effectiveDraft, targetText, nameTouched, channelsTouched });
const initialSnapshotRef = useRef<string | null>(null);
if (initialSnapshotRef.current === null) initialSnapshotRef.current = dirtySnapshot;
const isDirty = dirtySnapshot !== initialSnapshotRef.current;
const { attemptExit, dialog: dirtyDialog } = useDirtyGuard(isDirty);
```

(`useRef` joins the React import.)

**(d) Summary for the rail.** After `effectiveName`/`target` are computed:

```tsx
const railSummary = {
  title: isEdit ? "You are editing" : "You are creating",
  name: effectiveName,
  rule: targetingUniverse
    ? describeCoverage(draft.instruments, true, [])
    : describeRule(
        primaryInstrument.split(":").slice(1).join(":"),
        OPERATOR_LABELS[operator] ?? operator,
        targetValue,
      ),
  frequency: describeFrequency(effectiveDraft.alert),
  channels: selectedChannels,
};
```

Import `describeCoverage` from `@/features/alerts/lib/alert-state` and `describeRule` from `@/features/alerts/lib/plain-language` (both exist).

**(e) Header + grid.** Replace the opening `SectionLabel` block (lines ~391–400) with:

```tsx
<AlertsPageHeader
  backHref={isEdit ? `/alerts/${mode.workflowId}` : "/alerts"}
  onBack={attemptExit}
  trail={
    isEdit
      ? [{ label: "Alerts", href: "/alerts" }, { label: mode.workflowName, href: `/alerts/${mode.workflowId}` }, { label: "Edit" }]
      : [{ label: "Alerts", href: "/alerts" }, { label: "New alert" }]
  }
  right={
    <div role="tablist" aria-label="Editor view" className="flex gap-2">
      {/* the existing Form/Code view buttons move here verbatim */}
    </div>
  }
/>
<p className="text-sm text-muted-foreground">
  {isEdit
    ? "Saving creates a new draft revision; activation stays a separate, explicit step."
    : "Pick an instrument, set the level, choose where it notifies."}
</p>
```

**(f) Two-zone grid.** Wrap the form branch (the `<>` fragment at line ~518 containing the four panels) in:

```tsx
<div className="mx-auto grid w-full max-w-6xl gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
  <div className="flex min-w-0 flex-col gap-5">
    {/* instrument, rule, notifications, name sections — anchors added, see (g) */}
  </div>
  <aside className="lg:sticky lg:top-4 lg:self-start">
    <LiveSidePanel
      quote={quote}
      presentation={presentation}
      coverage={targetingUniverse ? describeCoverage(draft.instruments, true, []) : null}
      targetValue={targetValue}
      distance={target}
      validation={validation}
      summary={railSummary}
    />
  </aside>
</div>
```

The mobile inline price strip inside the instrument panel gains `className="lg:hidden"` on its wrapper (it already exists at lines ~552–578); the rail owns the price on `lg`+.

**(g) Section anchors.** Add `id` attributes: instrument panel `id="section-instrument"`, rule panel `id="section-rule"`, the Evaluation block `id="section-evaluation"` (it is inside the rule panel), the frequency panel `id="section-frequency"`, the destinations panel `id="section-destinations"`, the name panel `id="section-name"`.

**(h) Frequency segmented control.** Replace the stacked radio cards (lines ~734–756) with:

```tsx
<div role="radiogroup" aria-label="Notification frequency" className="flex w-fit flex-wrap overflow-hidden rounded-lg border border-border/60">
  {FREQUENCY_OPTIONS.map((option) => {
    const active = frequencyOf(draft.alert) === option.value;
    return (
      <label
        key={option.value}
        className={cn(
          "cursor-pointer border-r border-border/60 px-4 py-2 text-sm last:border-r-0",
          active ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
        )}
      >
        <input
          type="radio"
          name="alert-frequency"
          className="sr-only"
          checked={active}
          onChange={() => setDraft({ ...draft, alert: applyFrequency(draft.alert, option.value) })}
        />
        {option.label}
      </label>
    );
  })}
</div>
<p className="text-xs text-muted-foreground">
  {FREQUENCY_OPTIONS.find((option) => frequencyOf(draft.alert) === option.value)?.hint}
</p>
```

The enclosing `<Label>When should we notify you?</Label>` and the `describeFrequency` sentence + `SectionIssues` below it stay.

**(i) Destination chips.** Replace the bare checkbox list (lines ~893–920) with:

```tsx
<div className="flex flex-wrap gap-2">
  {enabledChannels.map((channel) => {
    const checked = selectedChannels.includes(channel.name);
    return (
      <label
        key={channel.channel_id}
        htmlFor={`destination-${channel.channel_id}`}
        className={cn(
          "flex cursor-pointer items-center gap-2 rounded-full border px-3 py-1.5 text-sm",
          checked ? "border-primary/60 bg-primary/10 text-primary" : "border-border/60 text-muted-foreground",
        )}
      >
        <Checkbox
          id={`destination-${channel.channel_id}`}
          checked={checked}
          onCheckedChange={() => {
            setChannelsTouched(true);
            setDraft({
              ...draft,
              alert: {
                ...draft.alert,
                channels: checked
                  ? selectedChannels.filter((name) => name !== channel.name)
                  : [...selectedChannels, channel.name],
              },
            });
          }}
        />
        {channel.name} · {channel.provider}
      </label>
    );
  })}
</div>
```

**(j) Save bar.** Keep the fixed bar and its buttons byte-for-byte. Its right-hand stack keeps: the validation `StatusBadge` (now using `VALIDATION_TONE`/`VALIDATION_LABEL`), the `evaluationLabel` + `validation.previewSentence` line, the local completeness `issues` list, the `validation.error` line. **Delete** the `sectionsWithIssues` list from the bar — per-section server issues now surface inline (unchanged) and in the rail (Task 3). Render `{dirtyDialog}` once, just before the closing `</div>` of the page.

- [ ] **Step 4: Update the existing tests where the DOM changed**

Only these, all in `unified-alert-editor.test.tsx`:

1. Any query that relied on all three frequency hints rendering at once → the selected option's hint only (no current test does; verify by run).
2. Any query that found per-section server issues in the save bar → they now render inline per section and in the rail's "What this alert will do" card (the rail's issue groups use `SECTION_META` labels). No current test queries these from the bar; verify by run.
3. Everything else — labels, aria-roles, texts listed in Global Constraints — is preserved by construction.

- [ ] **Step 5: Run the full editor test file**

Run: `npx vitest run features/alerts/components/unified-alert-editor.test.tsx`
Expected: PASS — all pre-existing tests plus the new workspace test.

- [ ] **Step 6: Typecheck**

Run: `npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add features/alerts/components/unified-alert-editor.tsx features/alerts/components/unified-alert-editor.test.tsx
git commit --no-gpg-sign -m "feat(alerts): two-zone editor workspace with live rail and dirty guard"
```

---

### Task 6: Navigation rollout to screener surfaces and detail pages

**Files:**
- Modify: `features/alerts/components/screener-editor.tsx`
- Modify: `features/alerts/components/quick-screener-composer.tsx`
- Modify: `app/(app)/alerts/screeners/new/page.tsx`
- Modify: `app/(app)/alerts/screeners/[workflowId]/edit/page.tsx`
- Modify: `features/alerts/components/workflow-detail-page.tsx` (~line 263–267)
- Modify: `features/alerts/components/screener-page.tsx` (~line 307–313)
- Modify: `features/alerts/components/universe-detail-page.tsx` (~line 67–70)

**Interfaces:**
- Consumes: `AlertsPageHeader` (Task 1). No new produces.

- [ ] **Step 1: Screener editor header**

In `screener-editor.tsx`, replace the `SectionLabel` block (lines ~133–137) with:

```tsx
<AlertsPageHeader
  backHref={isEditing ? `/alerts/screeners/${edit!.workflowId}` : "/alerts"}
  trail={
    isEditing
      ? [
          { label: "Alerts", href: "/alerts" },
          { label: "Screeners", href: "/alerts/screeners" },
          { label: draft.name || "Edit screener" },
        ]
      : [
          { label: "Alerts", href: "/alerts" },
          { label: "Screeners", href: "/alerts/screeners" },
          { label: "New screener" },
        ]
  }
/>
<p className="text-sm text-muted-foreground">
  A scheduled ranked scan. Results persist as runs; attachments notify on the transitions you choose.
</p>
```

Import `AlertsPageHeader`. (The guard wiring for this file is Task 7 — header first, guard after, so this task keeps tests green.)

- [ ] **Step 2: Quick screener composer header**

In `quick-screener-composer.tsx`, replace the `SectionLabel` block (lines ~182–186) with the same header, always the create trail (`Alerts / Screeners / New screener`, `backHref="/alerts"`).

- [ ] **Step 3: Route files**

- `app/(app)/alerts/screeners/new/page.tsx`: keep the "Back to the quick form" cross-link (it is a peer link, not a parent), but it now sits under the header rendered by `ScreenerEditor` — no change needed beyond verifying no duplicate/competing header.
- `app/(app)/alerts/screeners/[workflowId]/edit/page.tsx`: in BOTH branches — the `AdvancedDefinitionEditor` branch and the structured branch — render `AlertsPageHeader` with trail `Alerts / Screeners / {workflow.name} / Edit` and `backHref={`/alerts/screeners/${workflowId}`}` above the existing content. Remove the now-redundant leading note-position back affordances only if they duplicate the header (they do not — leave the YAML/JSON cross-link line).

- [ ] **Step 4: Detail pages**

Replace each "← All alerts" ghost-button block with the shared header:

- `workflow-detail-page.tsx`: trail `[{ Alerts, /alerts }, { workflow.name }]`, `backHref="/alerts"`.
- `screener-page.tsx`: trail `[{ Alerts, /alerts }, { Screeners, /alerts/screeners }, { workflow.name }]`, `backHref="/alerts"`.
- `universe-detail-page.tsx`: trail `[{ Alerts, /alerts }, { Universes, /alerts/universes }, { universe name }]`, `backHref="/alerts/universes"`.

Keep the badges/metadata rows and action buttons that followed the old back link; only the back affordance is replaced. The `SectionLabel` on these pages stays (the header replaces only the back link row; if `SectionLabel` and the header visually duplicate the title, drop the `SectionLabel` `title` prop's duplication by rendering the header above it — do not delete information).

- [ ] **Step 5: Run the affected tests + typecheck**

Run: `npx vitest run features/alerts/components/ && npm run typecheck`
Expected: PASS — `authoring-components.test.tsx`, `unified-alert-editor.test.tsx`, `alerts-list-page.test.tsx` unaffected; no type errors.

- [ ] **Step 6: Commit**

```bash
git add features/alerts/components/screener-editor.tsx features/alerts/components/quick-screener-composer.tsx "app/(app)/alerts/screeners/new/page.tsx" "app/(app)/alerts/screeners/[workflowId]/edit/page.tsx" features/alerts/components/workflow-detail-page.tsx features/alerts/components/screener-page.tsx features/alerts/components/universe-detail-page.tsx
git commit --no-gpg-sign -m "feat(alerts): shared header navigation across screener and detail surfaces"
```

---

### Task 7: Dirty guard on the screener editors

**Files:**
- Modify: `features/alerts/components/screener-editor.tsx`
- Modify: `features/alerts/components/quick-screener-composer.tsx`

**Interfaces:**
- Consumes: `useDirtyGuard` (Task 4), the `onBack` prop of `AlertsPageHeader` (Task 1).
- Produces: nothing new.

- [ ] **Step 1: Wire the guard in `screener-editor.tsx`**

After `const [draft, setDraft] = useState(...)` (line ~70):

```tsx
const initialDraftRef = useRef<ScreenerDraft | null>(null);
if (initialDraftRef.current === null) initialDraftRef.current = draft;
const isDirty = JSON.stringify(draft) !== JSON.stringify(initialDraftRef.current);
const { attemptExit, dialog: dirtyDialog } = useDirtyGuard(isDirty);
```

Then update the header from Task 6 to pass `onBack={attemptExit}` and render `{dirtyDialog}` before the page's closing `</div>`.

- [ ] **Step 2: Wire the guard in `quick-screener-composer.tsx`**

Same pattern after its `useState(draft)` (line ~71), snapshotting against the first render; pass `onBack={attemptExit}` to the header and render the dialog.

- [ ] **Step 3: Run tests + typecheck**

Run: `npx vitest run features/alerts/components/ && npm run typecheck`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add features/alerts/components/screener-editor.tsx features/alerts/components/quick-screener-composer.tsx
git commit --no-gpg-sign -m "feat(alerts): unsaved-work guard on the screener editors"
```

---

### Task 8: Full verification

**Files:** none created; verification only.

- [ ] **Step 1: Full unit suite**

Run: `npm test`
Expected: PASS — zero failures, zero skipped-by-hack.

- [ ] **Step 2: Lint + typecheck**

Run: `npm run lint && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Manual smoke (dev server, no push)**

Run `npm run dev` and walk the flow in a browser:

1. `/alerts/new` — two-zone layout on a wide window; pick an instrument → rail price card updates; type a target beyond the current price → insight card shows the "already past the level" sentence; segmented frequency switches; chips toggle; `Create and activate` navigates to the detail page.
2. Edit the created alert → header trail shows `Alerts / {name} / Edit`; make a change → click the header chevron → guard dialog appears; Keep editing → still on page; leave via dialog → back on detail.
3. Refresh mid-edit → browser's native "leave site?" prompt appears.
4. `/alerts/screeners/new` and an existing screener's edit → header present, guard active.
5. Narrow window (<1024px) → single column, inline price strip visible, rail below the name section.

Record anything that diverges from the spec in the task notes and fix before finishing; write the verification notes to `documents/` following the repo's `*-verification.md` convention.

- [ ] **Step 4: Final commit (verification notes)**

```bash
git add documents/
git commit --no-gpg-sign -m "docs(alerts): verification notes for the editor workspace redesign"
```

---

## Self-Review Record

- Spec coverage: §1 grid → Task 5(f); §2 sections/anchors → Task 5(b–h) + Task 3 maps; §3 rail cards → Task 2 + 3; §4 save bar → Task 5(j); §5 header + surfaces table → Tasks 1, 5(e), 6; §6 guard + limitation → Tasks 4, 5(c), 7; §7 file plan → matches; §8 testing → Tasks 1–5 steps + Task 8; §9 contracts → Global Constraints; §10 non-goals → Global Constraints.
- Placeholder scan: none — every code step carries its code; "verbatim moves" cite exact current line anchors and the code is already in the repo.
- Type consistency: `BreadcrumbItem`/`onBack` (T1) ↔ usage in T5/T6/T7; `PanelSummary` fields (T3) ↔ `railSummary` (T5d); `VALIDATION_*`/`SECTION_META` names (T3) ↔ imports in T5; `useDirtyGuard` return shape (T4) ↔ T5c/T7.
