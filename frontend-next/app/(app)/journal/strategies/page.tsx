"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, StrategyPerformance } from "@/lib/journal/types";
import { fetchStrategies } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { StrategyPerformanceTable } from "@/components/journal/strategy-performance-table";

export default function JournalStrategiesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const [strategies, setStrategies] = useState<StrategyPerformance[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;

    fetchStrategies()
      .then((d) => {
        if (!disposed) { setStrategies(d); }
      })
      .catch((e) => {
        if (!disposed) { setError(e instanceof Error ? e.message : "Failed to load strategies"); }
      })
      .finally(() => {
        if (!disposed) { setLoading(false); }
      });

    return () => { disposed = true; };
  }, []);

  return (
    <div className="space-y-4 pb-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <JournalNav />

      <StrategyPerformanceTable strategies={strategies} loading={loading} error={error} />
    </div>
  );
}
