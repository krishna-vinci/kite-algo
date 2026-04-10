"use client";

import { useEffect, useState } from "react";

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
  const [time, setTime] = useState(formatIstNow());

  useEffect(() => {
    const interval = window.setInterval(() => setTime(formatIstNow()), 1000);
    return () => window.clearInterval(interval);
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
      <span className="inline-flex items-center gap-1 rounded-[4px] border border-[rgba(52,211,153,.2)] bg-[var(--green-soft)] px-2 py-0.5 text-[10px] font-semibold text-[var(--green)]">
        <span className="h-[5px] w-[5px] rounded-full bg-[var(--green)]" />NIFTY auto
      </span>
      <span className="inline-flex items-center gap-1 rounded-[4px] border border-[rgba(52,211,153,.2)] bg-[var(--green-soft)] px-2 py-0.5 text-[10px] font-semibold text-[var(--green)]">
        <span className="h-[5px] w-[5px] rounded-full bg-[var(--green)]" />BNIFTY auto
      </span>
      <span className="flex-1" />
      <span className="rounded-[4px] border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)]">paper · IST</span>
      <span className="font-mono text-[11px] text-[var(--dim)]">{time}</span>
    </header>
  );
}
