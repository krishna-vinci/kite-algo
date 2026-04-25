"use client";

import { useEffect, useState } from "react";
import { StatusBadge } from "@/components/operator/status-badge";

import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { useMarketwatchQuotes } from "@/features/trading/hooks/use-marketwatch-quotes";
import { useRuntimeStatusQuery } from "@/features/trading/hooks/use-runtime-status-query";

function formatIstNow() {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

export function TopBar({ title }: Readonly<{ title: string }>) {
  const [time, setTime] = useState("--:--:--");
  const { quotes, connected } = useMarketwatchQuotes();
  const runtimeQuery = useRuntimeStatusQuery();
  const runtime = runtimeQuery.data;

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setTime(formatIstNow()));
    const interval = window.setInterval(() => setTime(formatIstNow()), 1000);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearInterval(interval);
    };
  }, []);

  return (
    <header className="flex h-[52px] items-center gap-3 border-b border-[var(--border)] bg-[var(--panel)] px-4 lg:px-5">
      <div className="min-w-0">
        <span className="block truncate text-sm font-semibold tracking-[0.08em] text-[var(--text)]">{title.toUpperCase()}</span>
        <span className="block text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">operator workspace</span>
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden" aria-live="polite">
        <MarketQuoteStrip quotes={quotes} compact />
      </div>
      <div className="hidden items-center gap-2 lg:flex">
        <StatusBadge tone={connected ? "positive" : "warning"}>{connected ? "market live" : "market reconnecting"}</StatusBadge>
        <StatusBadge tone={runtime?.brokerConnected ? "positive" : runtime ? "warning" : "neutral"}>
          {runtime ? `broker ${runtime.brokerStatus}` : "broker loading"}
        </StatusBadge>
      </div>
      <span className="font-mono text-xs text-[var(--dim)]">{time}</span>
    </header>
  );
}
