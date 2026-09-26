"use client";

import { useEffect, useState } from "react";
import { StatusBadge } from "@/components/operator/status-badge";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

import { MarketQuoteStrip } from "@/features/trading/components/market-quote-strip";
import { useMarketwatchQuotes } from "@/features/trading/hooks/use-marketwatch-quotes";
import { useRuntimeStatusQuery } from "@/features/trading/hooks/use-runtime-status-query";
import { usePlatformStatus } from "@/features/platform/hooks/use-platform-queries";
import { componentTone, componentTooltip, liveModeSummary } from "@/lib/platform/status";
import { cn } from "@/lib/utils";
import type { PlatformStatus } from "@/lib/platform/types";

const DOT_TONE_CLASSES = {
  positive: "bg-emerald-400",
  warning: "bg-amber-400",
  danger: "bg-rose-400",
  neutral: "bg-foreground/30",
} as const;

function StatusDot({
  label,
  state,
  detail,
}: Readonly<{ label: string; state: string | null | undefined; detail?: string | null }>) {
  const tone = componentTone(state);
  const tooltip = componentTooltip(label, state, detail);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="status"
          aria-label={tooltip}
          className={cn("inline-block h-2 w-2 rounded-full", DOT_TONE_CLASSES[tone])}
        />
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  );
}

function PlatformStatusStrip({ status }: Readonly<{ status: PlatformStatus }>) {
  return (
    <TooltipProvider>
      <div className="flex items-center gap-2">
        <StatusBadge tone={status.mode === "live" ? "danger" : "neutral"}>
          {liveModeSummary(status)}
        </StatusBadge>
        <div className="flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-background/40 px-2 py-1">
          <StatusDot label="Broker" state={status.broker.state} detail={status.broker.detail} />
          <StatusDot
            label="Market data"
            state={status.market_data.state}
            detail={status.market_data.last_tick_age_s != null ? `${status.market_data.last_tick_age_s}s old` : null}
          />
          <StatusDot
            label="Strategy runner"
            state={status.strategy_runner.state}
            detail={status.strategy_runner.last_seen_age_s != null ? `${status.strategy_runner.last_seen_age_s}s ago` : null}
          />
        </div>
      </div>
    </TooltipProvider>
  );
}

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
  const platformStatusQuery = usePlatformStatus();
  const platformStatus = platformStatusQuery.data;

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
        {platformStatus ? <PlatformStatusStrip status={platformStatus} /> : null}
        <StatusBadge tone={connected ? "positive" : "warning"}>{connected ? "market live" : "market reconnecting"}</StatusBadge>
        <StatusBadge tone={runtime?.brokerConnected ? "positive" : runtime ? "warning" : "neutral"}>
          {runtime ? `broker ${runtime.brokerStatus}` : "broker loading"}
        </StatusBadge>
      </div>
      <span className="font-mono text-xs text-[var(--dim)]">{time}</span>
    </header>
  );
}
