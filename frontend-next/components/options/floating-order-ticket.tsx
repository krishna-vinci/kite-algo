"use client";

import { useState } from "react";

type FloatingOrderTicketProps = Readonly<{
  open: boolean;
  initialStrike: number | null;
  initialOptionType: "call" | "put";
  initialSide: "long" | "short";
  onClose: () => void;
}>;

export function FloatingOrderTicket({ open, initialStrike, initialOptionType, initialSide, onClose }: FloatingOrderTicketProps) {
  const [optionType, setOptionType] = useState<"call" | "put">(initialOptionType);
  const [side, setSide] = useState<"long" | "short">(initialSide);
  const [strike, setStrike] = useState(initialStrike ?? 0);
  const [lots, setLots] = useState(1);

  return (
    <aside
      aria-hidden={!open}
      className={`absolute right-4 top-4 z-20 flex h-[28rem] w-[18rem] flex-col rounded-2xl border border-[var(--border)] bg-[var(--panel)] shadow-2xl transition ${open ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-8 opacity-0"}`}
    >
      <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2">
        <h3 className="text-xs font-semibold text-[var(--text)]">Quick Order</h3>
        <button type="button" onClick={onClose} className="cursor-pointer rounded border border-[var(--border)] px-2 py-1 text-[var(--muted)] transition-colors duration-200 hover:text-[var(--text)]">
          ×
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-auto px-3 py-3 text-[11px]">
        <div className="grid grid-cols-2 gap-2">
          <button type="button" onClick={() => setSide("long")} className={`cursor-pointer rounded border px-3 py-1.5 transition-colors duration-150 ${side === "long" ? "border-[var(--green)] text-[var(--green)]" : "border-[var(--border)] text-[var(--muted)]"}`}>
            BUY
          </button>
          <button type="button" onClick={() => setSide("short")} className={`cursor-pointer rounded border px-3 py-1.5 transition-colors duration-150 ${side === "short" ? "border-[var(--red)] text-[var(--red)]" : "border-[var(--border)] text-[var(--muted)]"}`}>
            SELL
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <button type="button" onClick={() => setOptionType("call")} className={`cursor-pointer rounded border px-3 py-1.5 transition-colors duration-150 ${optionType === "call" ? "border-[var(--accent)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}>
            CE
          </button>
          <button type="button" onClick={() => setOptionType("put")} className={`cursor-pointer rounded border px-3 py-1.5 transition-colors duration-150 ${optionType === "put" ? "border-[var(--accent)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}>
            PE
          </button>
        </div>
        <label className="flex flex-col gap-2 text-[var(--muted)]">
          Strike
          <input type="number" value={strike} onChange={(event) => setStrike(Number(event.currentTarget.value))} className="rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[var(--text)]" />
        </label>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg)]/70 p-2">
          <p className="text-[10px] uppercase tracking-[0.16em] text-[var(--dim)]">lots</p>
          <div className="mt-1 flex items-center gap-2">
            <button type="button" onClick={() => setLots((value) => Math.max(1, value - 1))} className="cursor-pointer rounded border border-[var(--border)] px-3 py-0.5 transition-colors duration-150">-</button>
            <span className="min-w-10 text-center text-sm font-semibold text-[var(--accent)]">{lots}</span>
            <button type="button" onClick={() => setLots((value) => value + 1)} className="cursor-pointer rounded border border-[var(--border)] px-3 py-0.5 transition-colors duration-150">+</button>
          </div>
        </div>
      </div>
      <div className="border-t border-[var(--border)] px-3 py-2">
        <button type="button" className="w-full cursor-pointer rounded-md border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 py-1.5 text-[11px] font-semibold text-[var(--accent)] transition-colors duration-200 hover:bg-[var(--accent-border)]">
          Dry-run {side === "long" ? "BUY" : "SELL"} {optionType === "call" ? "CE" : "PE"} {strike}
        </button>
      </div>
    </aside>
  );
}
