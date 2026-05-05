import { cn } from "@/lib/utils";

type KpiBandItemTone = "default" | "positive" | "negative" | "warning";

type KpiBandItem = {
  label: string;
  value: string;
  meta?: string;
  tone?: KpiBandItemTone;
};

type KpiBandProps = {
  items: KpiBandItem[];
  className?: string;
};

const toneClasses: Record<KpiBandItemTone, string> = {
  default: "text-foreground",
  positive: "text-[var(--green)]",
  negative: "text-[var(--red)]",
  warning: "text-amber-300",
};

export function KpiBand({ items, className }: KpiBandProps) {
  return (
    <section
      className={cn(
        "grid gap-px overflow-hidden rounded-[1.2rem] border border-border/55 bg-border/35 sm:grid-cols-2 xl:grid-cols-4",
        className,
      )}
    >
      {items.map((item) => (
        <article key={item.label} className="bg-card/70 px-4 py-4">
          <p className="text-[10px] uppercase tracking-[0.24em] text-foreground/40">{item.label}</p>
          <p className={cn("mt-3 font-mono text-[1.55rem] font-semibold tracking-tight", toneClasses[item.tone ?? "default"])}>
            {item.value}
          </p>
          {item.meta ? <p className="mt-1 text-xs text-foreground/55">{item.meta}</p> : null}
        </article>
      ))}
    </section>
  );
}
