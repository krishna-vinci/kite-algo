import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";

const activeAlerts = [
  { name: "NIFTY breakout", rule: "above 25,320", tone: "positive" as const, state: "armed" },
  { name: "BankNifty fade", rule: "below 51,500", tone: "warning" as const, state: "watching" },
  { name: "Premium crush", rule: "OI delta > 8%", tone: "neutral" as const, state: "queued" },
];

const history = [
  { time: "09:01", event: "NIFTY test alert fired", status: "sent" },
  { time: "08:47", event: "BankNifty alert paused", status: "paused" },
  { time: "08:22", event: "Premium trap cleared", status: "resolved" },
];

export default function AlertsPage() {
  return (
    <div className="grid gap-4 pb-4 xl:grid-cols-[0.95fr_1.05fr]">
      <Panel eyebrow="quick create" title="New alert">
        <div className="space-y-3 rounded-2xl border border-border/60 bg-background/60 p-4">
          <label className="block space-y-2">
            <span className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">symbol</span>
            <input defaultValue="NIFTY" className="w-full rounded-xl border border-border/70 bg-transparent px-3 py-2 font-mono text-sm outline-none" />
          </label>
          <label className="block space-y-2">
            <span className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">trigger</span>
            <input defaultValue="crosses above 25,320" className="w-full rounded-xl border border-border/70 bg-transparent px-3 py-2 font-mono text-sm outline-none" />
          </label>
          <div className="grid gap-2 sm:grid-cols-2">
            <button type="button" className="rounded-xl border border-border/70 px-3 py-2 text-sm">price alert</button>
            <button type="button" className="rounded-xl border border-primary/30 bg-primary/10 px-3 py-2 text-sm text-primary">create alert</button>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-2 text-xs">
          <StatusBadge tone="positive">sound on</StatusBadge>
          <StatusBadge tone="neutral">email off</StatusBadge>
          <StatusBadge tone="warning">throttle 30s</StatusBadge>
        </div>
      </Panel>

      <div className="space-y-4">
        <Panel eyebrow="active" title="Active alerts">
          <div className="space-y-3">
            {activeAlerts.map((alert) => (
              <div key={alert.name} className="rounded-2xl border border-border/60 bg-background/60 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-medium tracking-tight">{alert.name}</p>
                    <p className="mt-1 text-xs uppercase tracking-[0.3em] text-foreground/40">{alert.rule}</p>
                  </div>
                  <StatusBadge tone={alert.tone}>{alert.state}</StatusBadge>
                </div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel eyebrow="history" title="Alert history">
          <div className="space-y-3 font-mono text-sm">
            {history.map((entry) => (
              <div key={entry.time} className="flex items-center justify-between gap-4 rounded-xl border border-border/60 bg-background/60 px-3 py-2">
                <span className="text-foreground/40">{entry.time}</span>
                <span className="flex-1 text-right text-foreground/80">{entry.event}</span>
                <span className="text-primary">{entry.status}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
