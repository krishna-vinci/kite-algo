"use client";

import { useEffect, useState, useCallback } from "react";
import type { AnalysisPeriod, BenchmarkComparison, JournalRun, JournalSummary, ReviewQueueItem, CalendarDay } from "@/lib/journal/types";
import { fetchJournalSummary, fetchBenchmarkComparison, fetchReviewQueue, fetchJournalRuns, fetchCalendar } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { OverviewKpis } from "@/components/journal/overview-kpis";
import { BenchmarkComparisonCard } from "@/components/journal/benchmark-comparison-card";
import { ReviewQueueCard } from "@/components/journal/review-queue-card";
import { RecentRunsCard } from "@/components/journal/recent-runs-card";
import { CalendarHeatmap } from "@/components/journal/calendar-heatmap";
import { ReviewDrawer } from "@/components/journal/review-drawer";

export default function JournalOverviewPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const [summary, setSummary] = useState<JournalSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [benchmark, setBenchmark] = useState<BenchmarkComparison | null>(null);
  const [benchmarkLoading, setBenchmarkLoading] = useState(true);
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null);

  const [reviewItems, setReviewItems] = useState<ReviewQueueItem[]>([]);
  const [reviewLoading, setReviewLoading] = useState(true);
  const [reviewError, setReviewError] = useState<string | null>(null);

  const [runs, setRuns] = useState<JournalRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [calendarDays, setCalendarDays] = useState<CalendarDay[]>([]);
  const [calendarLoading, setCalendarLoading] = useState(true);
  const [calendarError, setCalendarError] = useState<string | null>(null);

  const [drawerRunId, setDrawerRunId] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setSummaryLoading(true);
    setBenchmarkLoading(true);
    setReviewLoading(true);
    setRunsLoading(true);
    setCalendarLoading(true);

    const params = { period };

    fetchJournalSummary(params)
      .then((d) => { setSummary(d); setSummaryError(null); })
      .catch((e) => { setSummaryError(e instanceof Error ? e.message : "Failed"); })
      .finally(() => setSummaryLoading(false));

    fetchBenchmarkComparison(params)
      .then((d) => { setBenchmark(d); setBenchmarkError(null); })
      .catch((e) => { setBenchmarkError(e instanceof Error ? e.message : "Failed"); })
      .finally(() => setBenchmarkLoading(false));

    fetchReviewQueue()
      .then((d) => { setReviewItems(d); setReviewError(null); })
      .catch((e) => { setReviewError(e instanceof Error ? e.message : "Failed"); })
      .finally(() => setReviewLoading(false));

    fetchJournalRuns({ page_size: 6 })
      .then((d) => { setRuns(d.items); setRunsError(null); })
      .catch((e) => { setRunsError(e instanceof Error ? e.message : "Failed"); })
      .finally(() => setRunsLoading(false));

    fetchCalendar()
      .then((d) => { setCalendarDays(d); setCalendarError(null); })
      .catch((e) => { setCalendarError(e instanceof Error ? e.message : "Failed"); })
      .finally(() => setCalendarLoading(false));
  }, [period]);

  useEffect(() => {
    let disposed = false;

    fetchJournalSummary({ period })
      .then((d) => {
        if (!disposed) {
          setSummary(d);
          setSummaryError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setSummaryError(e instanceof Error ? e.message : "Failed");
        }
      })
      .finally(() => {
        if (!disposed) {
          setSummaryLoading(false);
        }
      });

    fetchBenchmarkComparison({ period })
      .then((d) => {
        if (!disposed) {
          setBenchmark(d);
          setBenchmarkError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setBenchmarkError(e instanceof Error ? e.message : "Failed");
        }
      })
      .finally(() => {
        if (!disposed) {
          setBenchmarkLoading(false);
        }
      });

    fetchReviewQueue()
      .then((d) => {
        if (!disposed) {
          setReviewItems(d);
          setReviewError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setReviewError(e instanceof Error ? e.message : "Failed");
        }
      })
      .finally(() => {
        if (!disposed) {
          setReviewLoading(false);
        }
      });

    fetchJournalRuns({ page_size: 6 })
      .then((d) => {
        if (!disposed) {
          setRuns(d.items);
          setRunsError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setRunsError(e instanceof Error ? e.message : "Failed");
        }
      })
      .finally(() => {
        if (!disposed) {
          setRunsLoading(false);
        }
      });

    fetchCalendar()
      .then((d) => {
        if (!disposed) {
          setCalendarDays(d);
          setCalendarError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setCalendarError(e instanceof Error ? e.message : "Failed");
        }
      })
      .finally(() => {
        if (!disposed) {
          setCalendarLoading(false);
        }
      });

    return () => {
      disposed = true;
    };
  }, [period]);

  function handlePeriodChange(nextPeriod: AnalysisPeriod) {
    setSummaryLoading(true);
    setBenchmarkLoading(true);
    setReviewLoading(true);
    setRunsLoading(true);
    setCalendarLoading(true);
    setPeriod(nextPeriod);
  }

  return (
    <div className="space-y-4 pb-4">
      <JournalHeader period={period} onPeriodChange={handlePeriodChange} />
      <JournalNav />

      <OverviewKpis summary={summary} loading={summaryLoading} error={summaryError} />

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <BenchmarkComparisonCard data={benchmark} loading={benchmarkLoading} error={benchmarkError} />
        <ReviewQueueCard items={reviewItems} loading={reviewLoading} error={reviewError} onSelect={setDrawerRunId} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <RecentRunsCard runs={runs} loading={runsLoading} error={runsError} onSelect={setDrawerRunId} />
        <CalendarHeatmap days={calendarDays} loading={calendarLoading} error={calendarError} />
      </div>

      <ReviewDrawer runId={drawerRunId} onClose={() => setDrawerRunId(null)} onUpdated={loadAll} />
    </div>
  );
}
