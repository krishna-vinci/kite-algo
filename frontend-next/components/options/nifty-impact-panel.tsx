import type { NiftyImpactRow } from "@/components/options/types";

type NiftyImpactPanelProps = Readonly<{
  rows: NiftyImpactRow[];
}>;

export function NiftyImpactPanel({ rows }: NiftyImpactPanelProps) {
  const sortedRows = [...rows].sort((left, right) => right.weight - left.weight).slice(0, 16);
  const positive = sortedRows.filter((row) => (row.changePercent ?? 0) >= 0).reduce((sum, row) => sum + (row.weight ?? 0), 0);
  const negative = sortedRows.filter((row) => (row.changePercent ?? 0) < 0).reduce((sum, row) => sum + Math.abs(row.weight ?? 0), 0);

  return (
    <section className="flex h-full min-h-0 flex-col rounded-2xl border border-[var(--border)] bg-[var(--panel)]">
      <div className="border-b border-[var(--border)] px-3 py-2">
        <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">nifty 50 impact</p>
        <h2 className="mt-1 text-sm font-semibold text-[var(--text)]">Weightage-driven index contribution context</h2>
      </div>
      <div className="grid gap-3 px-3 py-3 lg:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-[var(--green)]">bull weight {positive.toFixed(2)}%</span>
            <span className="text-[var(--muted)]">vs</span>
            <span className="text-[var(--red)]">bear weight {negative.toFixed(2)}%</span>
          </div>
          <div className="mt-3 h-3 overflow-hidden rounded-full bg-[var(--red-soft)]">
            <div className="h-full bg-[var(--green)]" style={{ width: `${positive / Math.max(positive + negative, 1) * 100}%` }} />
          </div>
        </div>
        <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/70 p-3 text-[11px] text-[var(--muted)]">
          This panel uses the existing DB-backed Nifty 50 table to keep index-level context visible while trading options.
        </div>
      </div>
      <div className="overflow-auto px-3 pb-3">
        <table className="min-w-full border-collapse text-[11px]">
          <thead className="text-[9px] uppercase tracking-[0.16em] text-[var(--dim)]">
            <tr>
              <th className="px-2 py-2 text-left">Symbol</th>
              <th className="px-2 py-2 text-left">Sector</th>
              <th className="px-2 py-2 text-left">Weight</th>
              <th className="px-2 py-2 text-left">Change</th>
              <th className="px-2 py-2 text-left">Impact</th>
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row) => (
              <tr key={`${row.symbol}-${row.sector}`} className="border-t border-[var(--border-soft)]">
                <td className="px-2 py-2 font-medium text-[var(--text)]">{row.symbol}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{row.sector}</td>
                <td className="px-2 py-2 text-[var(--accent)]">{row.weight.toFixed(2)}%</td>
                <td className={`px-2 py-2 ${(row.changePercent ?? 0) >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
                  {row.changePercent === null || row.changePercent === undefined ? "—" : `${row.changePercent >= 0 ? "+" : ""}${row.changePercent.toFixed(2)}%`}
                </td>
                <td className={`px-2 py-2 ${(row.contribution ?? 0) >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
                  {row.contribution === null || row.contribution === undefined ? "—" : `${row.contribution >= 0 ? "+" : ""}${row.contribution.toFixed(2)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
