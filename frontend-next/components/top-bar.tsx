"use client";

import { useEffect, useState } from "react";

import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { useMarketwatchQuotes } from "@/features/trading/hooks/use-marketwatch-quotes";

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
  const { quotes } = useMarketwatchQuotes();

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setTime(formatIstNow()));
    const interval = window.setInterval(() => setTime(formatIstNow()), 1000);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearInterval(interval);
    };
  }, []);

  return (
    <header className="flex h-10 items-center gap-2 border-b border-[var(--border)] bg-[var(--panel)] px-4">
      <span className="text-[12px] font-bold tracking-[0.03em] text-[var(--text)]">{title.toUpperCase()}</span>
      <span className="mx-1 h-[18px] w-px bg-[var(--border)]" />
      <input
        readOnly
        aria-label="command palette"
        placeholder="⌘K  jump to anything..."
        className="h-7 w-[240px] rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 text-[11px] text-[var(--dim)] outline-none"
      />
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden" aria-live="polite">
        <MarketQuoteStrip quotes={quotes} compact />
      </div>
      <span className="rounded-[4px] border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)]">paper · IST</span>
      <span className="font-mono text-[11px] text-[var(--dim)]">{time}</span>
    </header>
  );
}
