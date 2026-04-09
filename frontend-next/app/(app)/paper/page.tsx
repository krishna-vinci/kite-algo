import { Panel } from "@/components/operator/panel";
import { SectionLabel } from "@/components/operator/section-label";
import { StatusBadge } from "@/components/operator/status-badge";
import { KpiCard } from "@/components/operator/kpi-card";

const accountOptions = ["Alpha / Live", "Paper / Intraday", "Sandbox / Hedge"];

const metrics = [
  { label: "Net P&L", value: "+₹42,180", delta: "+1.9%", note: "paper session drift" },
  { label: "Margin Used", value: "62%", delta: "-4%", note: "freed after hedge unwind" },
  { label: "Open Legs", value: "14", note: "grouped across 3 books" },
  { label: "Risk Buffer", value: "₹8.4L", delta: "stable", note: "protected by trailing stops" },
];

const positionGroups = [
  {
    title: "Index Hedge",
    summary: "2 legs • delta neutral • low churn",
    tone: "positive" as const,
    rows: [
      ["NIFTY 25300 CE", "Short", "20", "₹112.40", "₹2,248"],
      ["NIFTY 25200 PE", "Long", "20", "₹98.10", "₹1,962"],
    ],
  },
  {
    title: "Swing Inventory",
    summary: "4 legs • directional bias • review due 14:30",
    tone: "warning" as const,
    rows: [
      ["BANKNIFTY 54000 CE", "Long", "15", "₹201.55", "₹3,023"],
      ["FINNIFTY 22500 PE", "Short", "10", "₹88.20", "₹882"],
    ],
  },
];

const controlChips = ["Flatten group", "Refresh marks", "Roll strikes", "Export blotter"];

export default function PaperPage() {
  return (
    <div className="space-y-4 pb-4">
      <Panel
        eyebrow="paper"
        title="Account selector"
        action={<StatusBadge tone="positive">paper enabled</StatusBadge>}
      >
        <div className="flex flex-wrap gap-2">
          {accountOptions.map((account, index) => (
            <button
              key={account}
              type="button"
              className={[
                "rounded-full border px-3 py-2 text-xs font-medium uppercase tracking-[0.24em] transition-colors",
                index === 1
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/25 hover:text-foreground",
              ].join(" ")}
            >
              {account}
            </button>
          ))}
          <button
            type="button"
            className="rounded-full border border-dashed border-border/70 px-3 py-2 text-xs uppercase tracking-[0.24em] text-foreground/50"
          >
            + add account
          </button>
        </div>
      </Panel>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metrics.map((metric) => (
          <KpiCard key={metric.label} {...metric} />
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Panel eyebrow="positions" title="Grouped books">
          <div className="space-y-4">
            {positionGroups.map((group) => (
              <div key={group.title} className="rounded-[1.25rem] border border-border/70 bg-background/50 p-4">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <SectionLabel title={group.title} description={group.summary} />
                  <StatusBadge tone={group.tone}>{group.tone}</StatusBadge>
                </div>
                <div className="mt-4 overflow-hidden rounded-2xl border border-border/60">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-foreground/40">
                      <tr>
                        <th className="px-3 py-2 font-medium">Instrument</th>
                        <th className="px-3 py-2 font-medium">Side</th>
                        <th className="px-3 py-2 font-medium">Qty</th>
                        <th className="px-3 py-2 font-medium">LTP</th>
                        <th className="px-3 py-2 font-medium">MTM</th>
                      </tr>
                    </thead>
                    <tbody>
                      {group.rows.map((row) => (
                        <tr key={row[0]} className="border-t border-border/60 text-foreground/80">
                          {row.map((cell) => (
                            <td key={cell} className="px-3 py-3 font-mono text-sm">
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel eyebrow="controls" title="Risk and actions">
          <SectionLabel
            title="Execution controls"
            description="Static terminal controls keep the page focused on paper-session intent without implying live routing behavior."
          />
          <div className="mt-4 flex flex-wrap gap-2">
            {controlChips.map((chip, index) => (
              <button
                key={chip}
                type="button"
                className={[
                  "rounded-full border px-3 py-2 text-xs font-medium uppercase tracking-[0.24em] transition-colors",
                  index === 0
                    ? "border-primary/40 bg-primary/10 text-primary"
                    : "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/25 hover:text-foreground",
                ].join(" ")}
              >
                {chip}
              </button>
            ))}
          </div>

          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
              <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">Selection</p>
              <p className="mt-2 font-mono text-sm text-primary">Paper / Intraday</p>
            </div>
            <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
              <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">Guard</p>
              <p className="mt-2 font-mono text-sm text-primary">Trailing stop armed</p>
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}
