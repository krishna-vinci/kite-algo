import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

type StatusTone = "positive" | "warning" | "danger" | "neutral";

type StatusBadgeProps = Readonly<{
  tone?: StatusTone;
  children: ReactNode;
}>;

const toneClasses: Record<StatusTone, string> = {
  positive: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  warning: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  danger: "border-rose-400/30 bg-rose-400/10 text-rose-300",
  neutral: "border-border/70 bg-background/60 text-foreground/70",
};

export function StatusBadge({ tone = "neutral", children }: StatusBadgeProps) {
  return (
    <span className={cn("inline-flex items-center rounded-full border px-2.5 py-1 font-medium uppercase tracking-[0.24em]", toneClasses[tone])}>
      {children}
    </span>
  );
}
