"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { BarChart2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SectionTabs } from "@/components/shared/section-tabs";
import { DateNav } from "@/components/shared/date-nav";
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
  { href: "/analytics/strategies", label: "Strategy" },
] as const;

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
 *   - Title + Analytics link
 *   - ModeToggle + EnvironmentSelector from shared WorkspaceProvider
 *   - SectionTabs (Day, Week, Month, Strategies)
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

  // Current date from URL or today
  const currentDate = searchParams.get("date") ?? todayIso();

  // Env + mode from URL (used to build analytics link)
  const envParam = searchParams.get("env") ?? "";
  const modeParam = searchParams.get("mode") ?? "";

  // Build analytics link preserving current params
  const analyticsHref = buildParamHref("/journal/analytics", {
    env: envParam,
    mode: modeParam,
    date: isDayView ? currentDate : undefined,
  });

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
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2.5 text-xs text-muted-foreground"
            asChild
          >
            <Link href={analyticsHref}>
              <BarChart2Icon data-icon="inline-start" />
              Analytics
            </Link>
          </Button>
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
      </div>

      {/* ── Section tabs ─────────────────────────────────────────── */}
      <SectionTabs
        tabs={TABS as unknown as import("@/components/shared/section-tabs").SectionTab[]}
        preserveParams={["env", "mode", "date"]}
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
