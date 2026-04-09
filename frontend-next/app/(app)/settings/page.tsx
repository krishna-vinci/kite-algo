import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";

const sidebarItems = [
  ["Trading defaults", "active"],
  ["Protection defaults", "live"],
  ["Sessions", "3"],
  ["Notifications", "mock"],
];

const tradingDefaults = [
  ["Order mode", "NRML / intraday"],
  ["Autoslice", "enabled"],
  ["Re-entry", "2 attempts"],
  ["Default lot", "1 lot"],
];

const protectionDefaults = [
  ["Daily stop", "₹18,000"],
  ["Per-trade risk", "₹2,500"],
  ["Cooldown", "5 minutes"],
  ["Net exposure cap", "₹12L"],
];

const sessions = [
  ["Main desk", "09:15 - 15:25", "active"],
  ["Pre-open checks", "08:45 - 09:10", "scheduled"],
  ["Post-close replay", "15:40 - 16:00", "mock"],
];

const notifications = [
  ["Slack webhook", "connected"],
  ["Telegram bot", "mock"],
  ["In-app toast", "enabled"],
  ["Critical alert sound", "enabled"],
];

export default function SettingsPage() {
  return (
    <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)] pb-4">
      <aside className="rounded-[1.5rem] border border-border/70 bg-card/70 p-4 backdrop-blur">
        <SectionLabel eyebrow="settings" title="Workspace sections" description="Local navigation stays page-specific." />
        <nav aria-label="Settings sections" className="mt-4 space-y-2">
          {sidebarItems.map(([label, tag], index) => (
            <a
              key={label}
              href={`#${label.toLowerCase().replace(/\s+/g, "-")}`}
              aria-current={index === 0 ? "page" : undefined}
              className={[
                "flex items-center justify-between rounded-2xl border px-3 py-3 text-sm transition-colors",
                index === 0
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border/70 bg-background/60 text-foreground/75 hover:border-primary/25 hover:text-foreground",
              ].join(" ")}
            >
              <span className="font-medium tracking-tight">{label}</span>
              <StatusBadge tone={index === 0 ? "positive" : "neutral"}>{tag}</StatusBadge>
            </a>
          ))}
        </nav>
      </aside>

      <div className="space-y-4">
        <Panel id="trading-defaults" eyebrow="trading" title="Trading defaults" action={<StatusBadge tone="positive">locked</StatusBadge>}>
          <div className="grid gap-3 md:grid-cols-2">
            {tradingDefaults.map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
                <p className="mt-2 font-mono text-sm text-primary">{value}</p>
              </div>
            ))}
          </div>
        </Panel>

        <Panel id="protection-defaults" eyebrow="protection" title="Protection defaults" action={<StatusBadge tone="warning">mock policy</StatusBadge>}>
          <div className="grid gap-3 md:grid-cols-2">
            {protectionDefaults.map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
                <p className="mt-2 font-mono text-sm text-primary">{value}</p>
              </div>
            ))}
          </div>
        </Panel>

        <Panel id="sessions" eyebrow="sessions" title="Session configuration" action={<StatusBadge tone="neutral">3 schedules</StatusBadge>}>
          <div className="overflow-hidden rounded-2xl border border-border/60">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-foreground/40">
                <tr>
                  <th className="px-3 py-2 font-medium">Session</th>
                  <th className="px-3 py-2 font-medium">Window</th>
                  <th className="px-3 py-2 font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map(([session, window, state]) => (
                  <tr key={session} className="border-t border-border/60 text-foreground/80">
                    <td className="px-3 py-3 font-medium tracking-tight">{session}</td>
                    <td className="px-3 py-3 font-mono text-sm">{window}</td>
                    <td className="px-3 py-3">
                      <StatusBadge tone={state === "active" ? "positive" : state === "scheduled" ? "neutral" : "warning"}>
                        {state}
                      </StatusBadge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel id="notifications" eyebrow="notifications" title="Notification routing" action={<StatusBadge tone="neutral">mock wiring</StatusBadge>}>
          <div className="grid gap-3 md:grid-cols-2">
            {notifications.map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
                <p className="mt-2 font-mono text-sm text-primary">{value}</p>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
