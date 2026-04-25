"use client";

import { Panel } from "@/components/operator/panel";
import type { JournalTrade, Paginated } from "@/lib/journal/types";

type TradesTableProps = {
  data: Paginated<JournalTrade> | null;
  loading: boolean;
  error: string | null;
  page: number;
  onPageChange: (p: number) => void;
};

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

export function TradesTable({ data, loading, error, page, onPageChange }: TradesTableProps) {
  if (loading) {
    return (
      <Panel eyebrow="trades" title="Trade log">
        <div className="h-[200px] animate-pulse rounded-xl bg-background/40" />
      </Panel>
    );
  }

  if (error) {
    return (
      <Panel eyebrow="trades" title="Trade log">
        <p className="text-sm text-rose-300">Failed to load trades.</p>
      </Panel>
    );
  }

  if (!data || data.items.length === 0) {
    return (
      <Panel eyebrow="trades" title="Trade log">
        <p className="text-sm text-foreground/50">No trades recorded for this period.</p>
      </Panel>
    );
  }

  const totalPages = Math.ceil(data.total / data.page_size);

  return (
    <Panel eyebrow="trades" title="Trade log">
      <div className="overflow-hidden rounded-2xl border border-border/60">
        <table className="w-full text-left text-sm">
          <thead className="bg-muted/30 text-[10px] uppercase tracking-[0.28em] text-foreground/40">
            <tr>
              <th className="px-3 py-2 font-medium">Symbol</th>
              <th className="px-3 py-2 font-medium">Side</th>
              <th className="px-3 py-2 font-medium">Qty</th>
              <th className="px-3 py-2 font-medium">Price</th>
              <th className="px-3 py-2 font-medium">Charges</th>
              <th className="px-3 py-2 font-medium">Time</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((trade) => (
              <tr key={trade.id} className="border-t border-border/60 text-foreground/80">
                <td className="px-3 py-3 font-mono text-sm">{trade.tradingsymbol}</td>
                <td className={`px-3 py-3 text-sm font-medium ${trade.transaction_type === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>
                  {trade.transaction_type}
                </td>
                <td className="px-3 py-3 font-mono text-sm">{trade.quantity}</td>
                <td className="px-3 py-3 font-mono text-sm">₹{trade.price.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</td>
                <td className="px-3 py-3 font-mono text-sm text-foreground/50">
                  {trade.charges != null ? `₹${trade.charges.toFixed(2)}` : "—"}
                </td>
                <td className="px-3 py-3 text-sm text-foreground/50">{formatDate(trade.executed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="mt-3 flex items-center justify-between">
          <p className="text-xs text-foreground/40">
            Page {page} of {totalPages} ({data.total} trades)
          </p>
          <div className="flex gap-1">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => onPageChange(page - 1)}
              className="rounded-lg border border-border/70 bg-background/60 px-3 py-1.5 text-xs font-medium text-foreground/70 transition-colors hover:text-foreground disabled:opacity-30"
            >
              Prev
            </button>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => onPageChange(page + 1)}
              className="rounded-lg border border-border/70 bg-background/60 px-3 py-1.5 text-xs font-medium text-foreground/70 transition-colors hover:text-foreground disabled:opacity-30"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </Panel>
  );
}
