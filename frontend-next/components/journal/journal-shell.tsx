"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { BarChart2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SectionTabs } from "@/components/shared/section-tabs";
import { DateNav } from "@/components/shared/date-nav";
import { PeriodSelector, type Period } from "@/components/shared/period-selector";
import { ModeToggle } from "@/components/workspace/mode-toggle";
import { EnvironmentSelector } from "@/components/workspace/environment-selector";
import { useWorkspace } from "@/components/workspace/workspace-provider";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type JournalShellProps = {
  children: React.ReactNode;
};

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

const TABS = [
  { href: "/journal", label: "Day", exact: true },
  { href: "/journal/week", label: "Week" },
  { href: "/journal/month", label: "Month" },
] as const;

const ANALYTICS_TABS = [
  { href: "/journal/analytics", label: "Overview", exact: true },
  { href: "/journal/analytics/strategies", label: "Strategies" },
  { href: "/journal/analytics/equity", label: "Equity" },
  { href: "/journal/analytics/costs", label: "Costs" },
] as const;

const PERIODS: { value: Period; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "year", label: "Year" },
  { value: "since_inception", label: "All" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function todayIso(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * JournalShell — top-level layout for the Journal section.
 *
 * Provides:
 *   - Title + Review/Analytics mode switch
 *   - ModeToggle + EnvironmentSelector from shared WorkspaceProvider
 *   - SectionTabs for Review mode OR Analytics mode
 *   - DateNav shown only in day-view context (exact path /journal)
 */
export function JournalShell({ children }: JournalShellProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useRouter();

  const {
    environments,
    environmentsLoading,
    environmentsError,
    selectedMode,
    selectedEnvironmentId,
    setSelectedMode,
    setSelectedEnvironmentId,
  } = useWorkspace();

  const isDayView = pathname === "/journal";
  const isWeekView = pathname === "/journal/week";
  const isMonthView = pathname === "/journal/month";
  const isAnalyticsRoute = pathname.startsWith("/journal/analytics");

  // Current date from URL or today
  const currentDate = searchParams.get("date") ?? todayIso();

  // Env + mode from URL with workspace fallback (for bare /journal entry hydration)
  const envParam = searchParams.get("env") ?? selectedEnvironmentId ?? "";
  const modeParam = searchParams.get("mode") ?? selectedMode;
  const periodParam = searchParams.get("period") ?? "";
  const dateParam = searchParams.get("date") ?? "";
  const reviewParam = searchParams.get("review") ?? "";
  const periodParamValue = (searchParams.get("period") ?? "month") as Period;

  const currentReviewView = isWeekView ? "week" : isMonthView ? "month" : "day";
  const reviewTarget =
    reviewParam === "week" || reviewParam === "month" || reviewParam === "day"
      ? reviewParam
      : periodParam === "week"
        ? "week"
        : periodParam === "month"
          ? "month"
          : currentReviewView;

  const reviewBase =
    reviewTarget === "week"
      ? "/journal/week"
      : reviewTarget === "month"
        ? "/journal/month"
        : "/journal";

  // Build mode links preserving current params where sensible
  const analyticsHref = buildParamHref("/journal/analytics", {
    env: envParam,
    mode: modeParam,
    date: dateParam || (isDayView ? currentDate : undefined),
    period: periodParam || undefined,
    review: currentReviewView,
  });

  const reviewHref = buildParamHref(reviewBase, {
    env: envParam,
    mode: modeParam,
    date: dateParam || currentDate,
  });

  const handlePeriodChange = useCallback(
    (period: Period) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("period", period);
      router.push(`${pathname}?${params.toString()}`);
    },
    [pathname, router, searchParams],
  );

  // Handle date change in day view — push new URL
  const handleDateChange = useCallback(
    (newDate: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("date", newDate);
      router.push(`/journal?${params.toString()}`);
    },
    [router, searchParams],
  );

  // Env options filtered by mode
  const visibleEnvironments = environments.filter(
    (e) => (e.mode === "live" ? "live" : "paper") === selectedMode,
  );

  return (
    <div className="flex flex-col gap-0">
      {/* ── Top bar ─────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-1 pb-3 pt-1">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold tracking-tight">Journal</h2>
        </div>

        <div className="flex items-center gap-2">
          <div
            role="tablist"
            aria-label="Journal mode"
            className="inline-flex items-center rounded-lg border border-border/70 bg-background/40 p-1"
          >
            <Button
              variant={isAnalyticsRoute ? "ghost" : "secondary"}
              size="sm"
              className={cn(
                "h-7 px-2.5 text-xs",
                !isAnalyticsRoute && "shadow-sm",
              )}
              asChild
            >
              <Link href={reviewHref} role="tab" aria-selected={!isAnalyticsRoute}>
                Review
              </Link>
            </Button>
            <Button
              variant={isAnalyticsRoute ? "secondary" : "ghost"}
              size="sm"
              className={cn(
                "h-7 gap-1.5 px-2.5 text-xs",
                isAnalyticsRoute && "shadow-sm",
              )}
              asChild
            >
              <Link href={analyticsHref} role="tab" aria-selected={isAnalyticsRoute}>
                <BarChart2Icon data-icon="inline-start" />
                Analytics
              </Link>
            </Button>
          </div>
        </div>
      </div>

      {/* ── Controls row ─────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3 px-1 pb-3">
        <ModeToggle
          value={selectedMode}
          onValueChange={setSelectedMode}
          disabled={environmentsLoading}
        />

        <EnvironmentSelector
          environments={visibleEnvironments}
          selectedEnvironmentId={selectedEnvironmentId}
          onSelectEnvironment={setSelectedEnvironmentId}
          loading={environmentsLoading}
          error={environmentsError}
        />

        {isAnalyticsRoute ? (
          <PeriodSelector
            value={periodParamValue}
            onChange={handlePeriodChange}
            options={PERIODS}
          />
        ) : null}
      </div>

      {/* ── Section tabs ─────────────────────────────────────────── */}
      <SectionTabs
        tabs={
          (isAnalyticsRoute ? ANALYTICS_TABS : TABS) as unknown as import("@/components/shared/section-tabs").SectionTab[]
        }
        preserveParams={isAnalyticsRoute ? ["env", "mode", "date", "period", "review"] : ["env", "mode", "date"]}
        className="px-1"
      />

      {/* ── Date nav (day view only) ──────────────────────────────── */}
      {isDayView && (
        <div className="flex items-center gap-2 px-1 py-2">
          <DateNav
            date={currentDate}
            view="day"
            onChange={handleDateChange}
          />
        </div>
      )}

      <Separator className={cn(isDayView ? "mb-4" : "mb-4")} />

      {/* ── Page content ─────────────────────────────────────────── */}
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function buildParamHref(
  base: string,
  params: Record<string, string | undefined>,
): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) sp.set(k, v);
  }
  const qs = sp.toString();
  return qs ? `${base}?${qs}` : base;
}
