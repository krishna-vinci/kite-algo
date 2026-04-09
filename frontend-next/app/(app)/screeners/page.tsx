import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";

const savedScreeners = ["Breakout pulse", "Opening range", "IV crush hunt"];

const results = [
  { symbol: "NIFTY", price: "25,304.60", signal: "bullish", volume: "1.8x" },
  { symbol: "RELIANCE", price: "2,945.10", signal: "support hold", volume: "1.3x" },
  { symbol: "SBIN", price: "805.25", signal: "range break", volume: "2.2x" },
];

export default function ScreenersPage() {
  return (
    <div className="grid gap-4 pb-4 xl:grid-cols-[0.7fr_1.3fr]">
      <Panel eyebrow="saved" title="Saved screeners">
        <div className="space-y-2">
          {savedScreeners.map((screener, index) => (
            <div key={screener} className="flex items-center justify-between rounded-2xl border border-border/60 bg-background/60 px-4 py-3">
              <span className="font-medium tracking-tight">{screener}</span>
              <span className="font-mono text-xs text-foreground/40">0{index + 1}</span>
            </div>
          ))}
        </div>
      </Panel>

      <div className="space-y-4">
        <Panel eyebrow="builder" title="Filter builder">
          <div className="space-y-3 rounded-2xl border border-border/60 bg-background/60 p-4">
            <div className="flex flex-wrap gap-2 text-xs">
              <StatusBadge tone="neutral">price &gt; 200</StatusBadge>
              <StatusBadge tone="neutral">volume &gt; 1.5x</StatusBadge>
              <StatusBadge tone="warning">open interest rising</StatusBadge>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <button type="button" className="rounded-xl border border-border/70 px-3 py-2 text-sm">Add condition</button>
              <button type="button" className="rounded-xl border border-border/70 px-3 py-2 text-sm">Group OR</button>
              <button type="button" className="rounded-xl border border-primary/30 bg-primary/10 px-3 py-2 text-sm text-primary">Run screener</button>
            </div>
          </div>
        </Panel>

        <Panel eyebrow="results" title="Matched symbols">
          <div className="overflow-hidden rounded-2xl border border-border/60">
            <table className="w-full border-collapse text-left text-sm">
              <thead className="bg-background/70 text-[10px] uppercase tracking-[0.35em] text-foreground/40">
                <tr>
                  <th className="px-4 py-3">symbol</th>
                  <th className="px-4 py-3">price</th>
                  <th className="px-4 py-3">signal</th>
                  <th className="px-4 py-3">volume</th>
                </tr>
              </thead>
              <tbody>
                {results.map((row) => (
                  <tr key={row.symbol} className="border-t border-border/60 bg-card/50 font-mono">
                    <td className="px-4 py-3 text-primary">{row.symbol}</td>
                    <td className="px-4 py-3">{row.price}</td>
                    <td className="px-4 py-3">{row.signal}</td>
                    <td className="px-4 py-3">{row.volume}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}
