"use client";

import { useCallback } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { BookOpenIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SectionTabs } from "@/components/shared/section-tabs";
import { PeriodSelector, type Period } from "@/components/shared/period-selector";
import { ModeToggle } from "@/components/workspace/mode-toggle";
import { EnvironmentSelector } from "@/components/workspace/environment-selector";
import { useWorkspace } from "@/components/workspace/workspace-provider";

type AnalyticsShellProps = {
  children: React.ReactNode;
};

const TABS = [
  { href: "/analytics", label: "Dashboard", exact: true },
  { href: "/analytics/equity", label: "Equity Curve" },
  { href: "/analytics/costs", label: "Costs" },
  { href: "/analytics/strategies", label: "Strategy" },
] as const;

const PERIODS: { value: Period; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "year", label: "Year" },
  { value: "since_inception", label: "All" },
];

/**
 * AnalyticsShell — top-level layout for the Analytics section.
 *
 * Mirrors JournalShell structure: title row → controls row → tabs → separator → children.
 * Period is carried as a `period` search param; environment via `env`.
 */
export function AnalyticsShell({ children }: AnalyticsShellProps) {
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

  const currentPeriod = (searchParams.get("period") ?? "month") as Period;
  const envParam = searchParams.get("env") ?? "";
  const modeParam = searchParams.get("mode") ?? "";

  const journalHref = buildParamHref("/journal", { env: envParam, mode: modeParam });

  const handlePeriodChange = useCallback(
    (period: Period) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("period", period);
      router.push(`${pathname}?${params.toString()}`);
    },
    [pathname, router, searchParams],
  );

  const visibleEnvironments = environments.filter(
    (e) => (e.mode === "live" ? "live" : "paper") === selectedMode,
  );

  return (
    <div className="flex flex-col gap-0">
      <div className="flex flex-wrap items-center justify-between gap-3 px-1 pb-3 pt-1">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold tracking-tight">Analytics</h2>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2.5 text-xs text-muted-foreground"
            asChild
          >
            <Link href={journalHref}>
              <BookOpenIcon data-icon="inline-start" />
              Journal
            </Link>
          </Button>
        </div>
      </div>

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

        <PeriodSelector
          value={currentPeriod}
          onChange={handlePeriodChange}
          options={PERIODS}
        />
      </div>

      <SectionTabs
        tabs={TABS as unknown as import("@/components/shared/section-tabs").SectionTab[]}
        preserveParams={["env", "mode", "period"]}
        className="px-1"
      />

      <Separator className="mb-4" />

      {children}
    </div>
  );
}

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
