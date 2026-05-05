import { cn } from "@/lib/utils";

type KpiCardProps = Readonly<{
  label: string;
  value: string;
  delta?: string;
  note?: string;
  className?: string;
}>;

export function KpiCard({ label, value, delta, note, className }: KpiCardProps) {
  const deltaToneClass = delta
    ? delta.trim().startsWith("-")
      ? "text-[var(--red)]"
      : delta.trim().startsWith("+")
        ? "text-[var(--green)]"
        : "text-foreground/60"
    : "";

  return (
    <article className={cn("rounded-[1.1rem] border border-border/70 bg-background/60 p-4 shadow-[0_10px_30px_rgba(0,0,0,0.12)]", className)}>
      <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">{label}</p>
      <div className="mt-3 flex items-end justify-between gap-3">
        <p className="font-mono text-[1.7rem] font-semibold tracking-tight text-primary">{value}</p>
        {delta ? <p className={cn("text-sm font-medium", deltaToneClass)}>{delta}</p> : null}
      </div>
      {note ? <p className="mt-2 text-sm leading-6 text-foreground/60">{note}</p> : null}
    </article>
  );
}
