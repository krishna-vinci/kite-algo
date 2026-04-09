import { cn } from "@/lib/utils";

type KpiCardProps = Readonly<{
  label: string;
  value: string;
  delta?: string;
  note?: string;
  className?: string;
}>;

export function KpiCard({ label, value, delta, note, className }: KpiCardProps) {
  return (
    <article className={cn("rounded-[1.25rem] border border-border/70 bg-background/60 p-4", className)}>
      <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
      <div className="mt-3 flex items-end justify-between gap-3">
        <p className="font-mono text-2xl font-semibold tracking-tight text-primary">{value}</p>
        {delta ? <p className="text-xs font-medium text-emerald-400">{delta}</p> : null}
      </div>
      {note ? <p className="mt-2 text-xs leading-5 text-foreground/60">{note}</p> : null}
    </article>
  );
}
