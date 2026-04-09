import { KpiCard } from "@/components/operator/kpi-card";
import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";

const metrics = [
  { label: "Index", value: "25,304.60", delta: "+0.42%", note: "mock market snapshot" },
  { label: "Positions", value: "12", delta: "+2", note: "6 hedged / 4 open" },
  { label: "Latency", value: "18ms", delta: "-4ms", note: "gateway round trip" },
  { label: "Risk", value: "GREEN", note: "exposure within band" },
];

const feed = [
  { time: "09:15:02", message: "feed synced", tone: "positive" as const },
  { time: "09:15:07", message: "watchlist refreshed", tone: "neutral" as const },
  { time: "09:15:11", message: "risk checks clear", tone: "positive" as const },
  { time: "09:15:18", message: "awaiting trigger", tone: "warning" as const },
];

const watchlist = [
  { symbol: "NIFTY", bias: "bullish", price: "25,304.60" },
  { symbol: "BANKNIFTY", bias: "neutral", price: "51,682.20" },
  { symbol: "FINNIFTY", bias: "bearish", price: "24,207.85" },
];

export default function DashboardPage() {
  return (
    <div className="space-y-4 pb-4">
      <Panel eyebrow="dashboard" title="Operator overview">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {metrics.map((item) => (
            <KpiCard key={item.label} {...item} />
          ))}
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel eyebrow="feed" title="Execution stream">
          <div className="space-y-3 font-mono text-sm">
            {feed.map((entry) => (
              <div key={entry.time} className="flex items-center justify-between gap-4 rounded-xl border border-border/60 bg-background/60 px-3 py-2">
                <span className="text-foreground/40">{entry.time}</span>
                <span className="flex-1 text-right text-foreground/80">{entry.message}</span>
                <StatusBadge tone={entry.tone}>{entry.tone}</StatusBadge>
              </div>
            ))}
          </div>
        </Panel>

        <Panel eyebrow="watchlist" title="Pinned symbols">
          <div className="space-y-3">
            {watchlist.map((item) => (
              <div key={item.symbol} className="flex items-center justify-between gap-3 rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
                <div>
                  <p className="font-mono text-sm font-semibold tracking-[0.2em] text-primary">{item.symbol}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.3em] text-foreground/40">{item.bias}</p>
                </div>
                <p className="font-mono text-base text-foreground/80">{item.price}</p>
              </div>
            ))}
          </div>
          <SectionLabel
            className="mt-4"
            eyebrow="session"
            title="Locked console state"
            description="Dashboard content stays static and mock-driven while the shell keeps the terminal-style frame consistent across routes."
          />
        </Panel>
      </div>
    </div>
  );
}
