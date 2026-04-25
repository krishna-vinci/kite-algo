import { cn } from "@/lib/utils";

type SectionLabelProps = Readonly<{
  eyebrow?: string;
  title: string;
  description?: string;
  className?: string;
}>;

export function SectionLabel({ eyebrow, title, description, className }: SectionLabelProps) {
  return (
    <div className={cn("space-y-1", className)}>
      {eyebrow ? <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">{eyebrow}</p> : null}
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {description ? <p className="max-w-2xl text-sm leading-6 text-foreground/60">{description}</p> : null}
    </div>
  );
}
