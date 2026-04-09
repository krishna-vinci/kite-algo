import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";

const chainRows = [
  { strike: "25,200", ceLtp: "118.20", ceDelta: "0.42", peLtp: "86.10", peDelta: "-0.38" },
  { strike: "25,250", ceLtp: "92.05", ceDelta: "0.36", peLtp: "105.70", peDelta: "-0.31" },
  { strike: "25,300", ceLtp: "71.50", ceDelta: "0.28", peLtp: "129.15", peDelta: "-0.24" },
  { strike: "25,350", ceLtp: "55.40", ceDelta: "0.21", peLtp: "154.90", peDelta: "-0.17" },
];

function Stepper({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
      <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
      <div className="mt-3 flex items-center gap-2 font-mono">
        <button type="button" className="rounded-lg border border-border/70 px-3 py-1.5 text-sm text-foreground/80">-</button>
        <span className="min-w-16 text-center text-base text-primary">{value}</span>
        <button type="button" className="rounded-lg border border-border/70 px-3 py-1.5 text-sm text-foreground/80">+</button>
      </div>
    </div>
  );
}

export default function OptionsPage() {
  return (
    <div className="space-y-4 pb-28">
      <Panel eyebrow="options" title="Strategy builder" action={<StatusBadge tone="warning">bottom-dock ready</StatusBadge>}>
        <div className="grid gap-3 lg:grid-cols-[1.1fr_0.9fr]">
          <div className="space-y-3">
            <div className="grid gap-3 md:grid-cols-3">
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">strategy</p>
                <button type="button" className="mt-3 flex w-full items-center justify-between rounded-xl border border-border/70 px-3 py-2 text-left text-sm">
                  <span>Short straddle</span>
                  <span className="text-foreground/40">trigger</span>
                </button>
              </div>
              <Stepper label="strike" value="25,300" />
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">lot</p>
                <div className="mt-3 flex flex-wrap items-center gap-2 font-mono">
                  <button type="button" className="rounded-lg border border-border/70 px-3 py-1.5 text-sm text-foreground/80">-</button>
                  <span className="min-w-12 text-center text-base text-primary">1</span>
                  <button type="button" className="rounded-lg border border-border/70 px-3 py-1.5 text-sm text-foreground/80">+</button>
                  <button type="button" className="rounded-lg border border-primary/30 bg-primary/10 px-3 py-1.5 text-sm text-primary">×2</button>
                </div>
              </div>
            </div>

            <div className="grid gap-3 md:grid-cols-[1fr_auto]">
              <label className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <span className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">delta search</span>
                <input
                  aria-label="delta search"
                  defaultValue="0.30"
                  className="mt-3 w-full bg-transparent font-mono text-sm text-foreground outline-none"
                />
              </label>
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">expiry</p>
                <button type="button" className="mt-3 flex min-w-44 items-center justify-between rounded-xl border border-border/70 px-3 py-2 text-sm">
                  <span>28 Apr 2026</span>
                  <span className="text-foreground/40">dropdown</span>
                </button>
              </div>
            </div>
          </div>

          <div className="rounded-[1.5rem] border border-border/60 bg-background/60 p-4">
            <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">protection strip</p>
            <div className="mt-3 flex flex-wrap gap-2 text-xs">
              <StatusBadge tone="positive">hedge on</StatusBadge>
              <StatusBadge tone="neutral">stop 1.25x</StatusBadge>
              <StatusBadge tone="neutral">trail active</StatusBadge>
              <StatusBadge tone="warning">manual review</StatusBadge>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">risk</p>
                <p className="mt-2 font-mono text-lg text-primary">0.78%</p>
              </div>
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">buffer</p>
                <p className="mt-2 font-mono text-lg text-primary">₹14,500</p>
              </div>
              <div className="rounded-2xl border border-border/60 bg-background/60 p-3">
                <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">mode</p>
                <p className="mt-2 font-mono text-lg text-primary">paper</p>
              </div>
            </div>
          </div>
        </div>
      </Panel>

      <Panel eyebrow="chain" title="Option chain">
        <div className="overflow-hidden rounded-2xl border border-border/60">
          <table className="w-full border-collapse text-left text-sm">
            <thead className="bg-background/70 text-[10px] uppercase tracking-[0.35em] text-foreground/40">
              <tr>
                <th className="px-4 py-3">strike</th>
                <th className="px-4 py-3">ce ltp</th>
                <th className="px-4 py-3">ce delta</th>
                <th className="px-4 py-3">pe ltp</th>
                <th className="px-4 py-3">pe delta</th>
              </tr>
            </thead>
            <tbody>
              {chainRows.map((row) => (
                <tr key={row.strike} className="border-t border-border/60 bg-card/50 font-mono">
                  <td className="px-4 py-3 text-primary">{row.strike}</td>
                  <td className="px-4 py-3">{row.ceLtp}</td>
                  <td className="px-4 py-3 text-emerald-300">{row.ceDelta}</td>
                  <td className="px-4 py-3">{row.peLtp}</td>
                  <td className="px-4 py-3 text-rose-300">{row.peDelta}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
