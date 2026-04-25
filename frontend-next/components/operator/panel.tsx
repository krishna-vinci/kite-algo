import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

type PanelProps = HTMLAttributes<HTMLElement> & {
  eyebrow?: string;
  title?: string;
  action?: ReactNode;
};

export function Panel({ eyebrow, title, action, className, children, ...props }: PanelProps) {
  return (
    <section
      className={cn("rounded-[1.35rem] border border-border/70 bg-card/80 p-5 shadow-[0_18px_40px_rgba(0,0,0,0.18)] backdrop-blur", className)}
      {...props}
    >
      {(eyebrow || title || action) && (
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            {eyebrow ? <p className="text-[11px] uppercase tracking-[0.28em] text-foreground/40">{eyebrow}</p> : null}
            {title ? <h3 className="mt-2 text-lg font-semibold tracking-tight text-foreground">{title}</h3> : null}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
