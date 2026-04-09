import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";
import { KpiCard } from "@/components/operator/kpi-card";

const kpis = [
  { label: "Index", value: "25,304.60", delta: "+0.42%", note: "mock market snapshot" },
  { label: "Latency", value: "18ms", delta: "-4ms", note: "gateway round trip" },
  { label: "Health", value: "ONLINE", note: "runtime and query client ready" },
  { label: "Alerts", value: "03", delta: "stable", note: "thresholds armed" },
];

const lanes = [
  {
    title: "Canvas",
    note: "main market view, chart stack, or future builder output",
  },
  {
    title: "Command rail",
    note: "controls and session actions without committing the final workflow",
  },
  {
    title: "Status lane",
    note: "workspace health, risk signals, and static metadata",
  },
];

const stream = [
  { time: "09:15:02", message: "feed synced", tone: "positive" as const },
  { time: "09:15:07", message: "workspace zones aligned", tone: "neutral" as const },
  { time: "09:15:11", message: "layout still editable", tone: "positive" as const },
  { time: "09:15:18", message: "builder behavior deferred", tone: "warning" as const },
];

export default function CustomDisplayPage() {
  return (
    <div className="space-y-4 pb-4">
      <Panel eyebrow="custom display" title="Operator overview">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {kpis.map((item) => (
            <KpiCard key={item.label} {...item} />
          ))}
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel eyebrow="layout" title="Workspace composition">
          <div className="grid gap-3 md:grid-cols-3">
            {lanes.map((lane) => (
              <div key={lane.title} className="rounded-[1.25rem] border border-border/60 bg-background/60 p-4">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{lane.title}</p>
                <p className="mt-2 text-sm leading-6 text-foreground/70">{lane.note}</p>
              </div>
            ))}
          </div>

          <div className="mt-4 rounded-[1.25rem] border border-dashed border-border/60 bg-background/40 p-4">
            <SectionLabel
              title="Not a final builder contract"
              description="This page only sketches how a future builder could compose panels, feeds, and command surfaces. The implementation remains intentionally open-ended."
            />
          </div>
        </Panel>

        <Panel eyebrow="feed" title="Terminal stream">
          <div className="space-y-3 font-mono text-sm">
            {stream.map((entry) => (
              <div key={entry.time} className="flex items-center justify-between gap-4 rounded-xl border border-border/60 bg-background/60 px-3 py-2">
                <span className="text-foreground/40">{entry.time}</span>
                <span className="flex-1 text-right text-foreground/80">{entry.message}</span>
                <StatusBadge tone={entry.tone}>{entry.tone}</StatusBadge>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  );
}
