type BottomDockProps = Readonly<{
  workspace: string;
}>;

export function BottomDock({ workspace }: BottomDockProps) {
  return (
    <footer className="border-t border-[var(--border)] bg-[var(--panel)]">
      <div className="flex items-center gap-2 border-b border-[var(--border-soft)] px-4 py-1.5 text-[11px]">
        <span className="rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-[var(--text)]">positions</span>
        <span className="rounded-md px-2 py-1 text-[var(--dim)]">orders</span>
        <span className="rounded-md px-2 py-1 text-[var(--dim)]">fills</span>
        <span className="flex-1" />
        <span className="text-[9px] uppercase tracking-[0.08em] text-[var(--dim)]">workspace</span>
        <span className="text-[10px] text-[var(--muted)]">{workspace}</span>
        <span className="ml-3 text-[9px] uppercase tracking-[0.08em] text-[var(--dim)]">day p/l</span>
        <span className="font-semibold text-[var(--green)]">+7,180</span>
      </div>
      <div className="overflow-x-auto px-4 py-2 text-[11px]">
        <div className="grid min-w-[780px] grid-cols-[160px_1fr_80px_80px_80px_100px] gap-3 text-[var(--muted)]">
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">strategy</span>
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">instrument</span>
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">qty</span>
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">avg</span>
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">ltp</span>
          <span className="text-[9px] uppercase tracking-[0.06em] text-[var(--dim)]">p/l</span>
          <span className="rounded bg-[var(--accent-soft)] px-2 py-1 text-[9px] font-bold text-[var(--accent)]">Short Straddle</span>
          <span>NIFTY 22550 CE</span>
          <span>-100</span>
          <span>268.90</span>
          <span>255.40</span>
          <span className="text-[var(--green)]">+1,350</span>
        </div>
      </div>
    </footer>
  );
}
