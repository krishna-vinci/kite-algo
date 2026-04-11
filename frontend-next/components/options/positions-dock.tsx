"use client";

import { useState } from "react";
import type { LivePosition } from "@/components/options/types";

type PositionsDockProps = Readonly<{
  positions: LivePosition[];
  expanded: boolean;
  onToggle: () => void;
}>;

const badgeTone: Record<LivePosition["badge"], string> = {
  strategy: "text-[var(--blue)] border-[var(--blue)]/30",
  naked: "text-[var(--yellow)] border-[var(--yellow)]/30",
  manual: "text-[var(--accent)] border-[var(--accent-border)]",
  algo: "text-[var(--green)] border-[var(--green)]/30",
  unmanaged: "text-[var(--muted)] border-[var(--border)]",
};

const dockTabs = ["positions", "orders", "fills", "paper", "dry-run"] as const;

export function PositionsDock({ positions, expanded, onToggle }: PositionsDockProps) {
  const [activeTab, setActiveTab] = useState<(typeof dockTabs)[number]>("positions");
  const totalPnl = positions.reduce((sum, position) => sum + position.pnl, 0);

  return (
    <section className="flex-none rounded-2xl border border-[var(--border)] bg-[var(--panel)]">
      <button type="button" onClick={onToggle} aria-expanded={expanded} className="flex w-full cursor-pointer items-center gap-3 px-3 py-2 text-left transition-colors duration-150 hover:bg-[var(--panel-hover)]">
        <span className="text-[10px] uppercase tracking-[0.18em] text-[var(--dim)]">dock</span>
        <div className="flex flex-1 flex-wrap gap-2 overflow-hidden">
          {positions.slice(0, 5).map((position) => (
            <span key={position.key} className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-[11px] text-[var(--text)]">
              {position.tradingSymbol} <span className={position.pnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}>{position.pnl >= 0 ? "+" : ""}{position.pnl.toFixed(0)}</span>
            </span>
          ))}
        </div>
        <span className={`text-sm font-semibold ${totalPnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>{totalPnl >= 0 ? "+" : ""}₹{totalPnl.toFixed(0)}</span>
      </button>
      {expanded ? (
        <div className="border-t border-[var(--border)] px-3 pb-3 pt-2">
          <div className="mb-3 flex flex-wrap gap-2">
            {dockTabs.map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setActiveTab(tab)}
                className={`rounded-md border px-2 py-1 text-[10px] uppercase tracking-[0.14em] ${activeTab === tab ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}
              >
                {tab}
              </button>
            ))}
          </div>

          {activeTab === "positions" ? (
            <table className="min-w-full border-collapse text-[11px]">
              <thead className="text-[9px] uppercase tracking-[0.16em] text-[var(--dim)]">
                <tr>
                  <th className="px-2 py-2 text-left">Instrument</th>
                  <th className="px-2 py-2 text-left">Badge</th>
                  <th className="px-2 py-2 text-left">Qty</th>
                  <th className="px-2 py-2 text-left">Avg</th>
                  <th className="px-2 py-2 text-left">LTP</th>
                  <th className="px-2 py-2 text-left">P/L</th>
                </tr>
              </thead>
              <tbody>
                {positions.length === 0 ? (
                  <tr><td colSpan={6} className="px-2 py-4 text-center text-[var(--muted)]">no positions</td></tr>
                ) : positions.map((position) => (
                  <tr key={position.key} className="border-t border-[var(--border-soft)]">
                    <td className="px-2 py-2 text-[var(--text)]">{position.tradingSymbol}</td>
                    <td className="px-2 py-2"><span className={`rounded border px-2 py-0.5 text-[9px] uppercase tracking-[0.12em] ${badgeTone[position.badge]}`}>{position.badge}</span></td>
                    <td className="px-2 py-2 text-[var(--muted)]">{position.quantity}</td>
                    <td className="px-2 py-2 text-[var(--muted)]">{position.averagePrice.toFixed(2)}</td>
                    <td className="px-2 py-2 text-[var(--text)]">{position.lastPrice.toFixed(2)}</td>
                    <td className={`px-2 py-2 ${position.pnl >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>{position.pnl >= 0 ? "+" : ""}{position.pnl.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="rounded-xl border border-[var(--border-soft)] bg-[var(--bg)]/60 px-3 py-4 text-center text-[11px] text-[var(--muted)]">
              {activeTab} — coming soon
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}
