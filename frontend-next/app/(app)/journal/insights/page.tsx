"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, JournalInsight } from "@/lib/journal/types";
import { fetchInsights } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { InsightsFeed } from "@/components/journal/insights-feed";

export default function JournalInsightsPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const [insights, setInsights] = useState<JournalInsight[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;

    fetchInsights()
      .then((d) => {
        if (!disposed) { setInsights(d); }
      })
      .catch((e) => {
        if (!disposed) { setError(e instanceof Error ? e.message : "Failed to load insights"); }
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

      <InsightsFeed insights={insights} loading={loading} error={error} />
    </div>
  );
}
