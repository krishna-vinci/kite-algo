"use client";

import { useEffect, useState } from "react";
import type { AnalysisPeriod, JournalTrade, Paginated } from "@/lib/journal/types";
import { fetchTrades } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { TradesTable } from "@/components/journal/trades-table";

export default function JournalTradesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const [page, setPage] = useState(1);

  const [data, setData] = useState<Paginated<JournalTrade> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;

    fetchTrades({ page, page_size: 25 })
      .then((d) => {
        if (!disposed) { setData(d); }
      })
      .catch((e) => {
        if (!disposed) { setError(e instanceof Error ? e.message : "Failed to load trades"); }
      })
      .finally(() => {
        if (!disposed) { setLoading(false); }
      });

    return () => { disposed = true; };
  }, [page]);

  return (
    <div className="space-y-4 pb-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <JournalNav />

      <TradesTable
        data={data}
        loading={loading}
        error={error}
        page={page}
        onPageChange={(nextPage) => {
          setLoading(true);
          setError(null);
          setPage(nextPage);
        }}
      />
    </div>
  );
}
