import type { MiniChainSnapshot, Underlying } from "@/components/options/types";

type OptionChainPanelProps = Readonly<{
  underlying: Underlying;
  expiry: string;
  expiries: string[];
  onExpiryChange: (value: string) => void;
  deltaFilter: number;
  onDeltaFilterChange: (value: number) => void;
  chain: MiniChainSnapshot | null;
  loading: boolean;
  onQuickOrder: (payload: { strike: number; optionType: "call" | "put"; side: "long" | "short" }) => void;
}>;

function formatMetric(value?: number | null, digits = 2) {
  if (value === undefined || value === null) {
    return "—";
  }
  return value.toFixed(digits);
}

export function OptionChainPanel({
  underlying,
  expiry,
  expiries,
  onExpiryChange,
  deltaFilter,
  onDeltaFilterChange,
  chain,
  loading,
  onQuickOrder,
}: OptionChainPanelProps) {
  const filteredRows = (chain?.strikes ?? []).filter((row) => {
    const deltas = [Math.abs(row.ce?.delta ?? 0), Math.abs(row.pe?.delta ?? 0)];
    return deltas.some((value) => value >= deltaFilter - 0.12 && value <= deltaFilter + 0.22) || row.isAtm;
  });

  return (
    <section className="flex h-full min-h-0 flex-col rounded-2xl border border-[var(--border)] bg-[var(--panel)]">
      <div className="flex flex-wrap items-center gap-3 border-b border-[var(--border)] px-3 py-2">
        <div>
          <p className="text-[10px] uppercase tracking-[0.24em] text-[var(--dim)]">option chain</p>
          <h2 className="mt-1 text-sm font-semibold text-[var(--text)]">{underlying} live chain</h2>
        </div>
        <label className="ml-auto flex items-center gap-2 text-[11px] text-[var(--muted)]">
          expiry
          <select value={expiry} onChange={(event) => onExpiryChange(event.currentTarget.value)} className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-[var(--text)]">
            {expiries.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-[11px] text-[var(--muted)]">
          delta filter
          <input
            aria-label="delta filter"
            type="number"
            min={0.05}
            max={0.95}
            step={0.05}
            value={deltaFilter}
            onChange={(event) => onDeltaFilterChange(Number(event.currentTarget.value))}
            className="w-20 rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-[var(--text)]"
          />
        </label>
      </div>
      <div className="overflow-auto px-3 py-3">
        {loading ? <p className="text-[var(--muted)]">Loading live chain…</p> : null}
        <table className="min-w-full border-collapse text-[11px]">
          <thead className="sticky top-0 bg-[var(--panel)] text-[9px] uppercase tracking-[0.16em] text-[var(--dim)]">
            <tr>
              <th className="px-2 py-2 text-left">CE</th>
              <th className="px-2 py-2 text-left">Δ</th>
              <th className="px-2 py-2 text-left">Γ</th>
              <th className="px-2 py-2 text-left">Θ</th>
              <th className="px-2 py-2 text-left">Vega</th>
              <th className="px-2 py-2 text-left">Strike</th>
              <th className="px-2 py-2 text-left">PE</th>
              <th className="px-2 py-2 text-left">Δ</th>
              <th className="px-2 py-2 text-left">Γ</th>
              <th className="px-2 py-2 text-left">Θ</th>
              <th className="px-2 py-2 text-left">Vega</th>
            </tr>
          </thead>
          <tbody>
            {filteredRows.map((row) => (
              <tr key={row.strike} className={`border-t border-[var(--border-soft)] ${row.isAtm ? "bg-[var(--accent-soft)]/60" : ""}`}>
                <td className="px-2 py-2">
                  <button type="button" onClick={() => onQuickOrder({ strike: row.strike, optionType: "call", side: "long" })} className="rounded border border-[var(--border)] px-2 py-1 text-left text-[var(--text)] hover:border-[var(--accent)]">
                    {formatMetric(row.ce?.ltp)}
                  </button>
                </td>
                <td className="px-2 py-2 text-[var(--green)]">{formatMetric(row.ce?.delta)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.ce?.gamma, 3)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.ce?.theta, 2)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.ce?.vega, 2)}</td>
                <td className="px-2 py-2 font-semibold text-[var(--accent)]">{row.strike}</td>
                <td className="px-2 py-2">
                  <button type="button" onClick={() => onQuickOrder({ strike: row.strike, optionType: "put", side: "long" })} className="rounded border border-[var(--border)] px-2 py-1 text-left text-[var(--text)] hover:border-[var(--accent)]">
                    {formatMetric(row.pe?.ltp)}
                  </button>
                </td>
                <td className="px-2 py-2 text-[var(--red)]">{formatMetric(row.pe?.delta)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.pe?.gamma, 3)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.pe?.theta, 2)}</td>
                <td className="px-2 py-2 text-[var(--muted)]">{formatMetric(row.pe?.vega, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
