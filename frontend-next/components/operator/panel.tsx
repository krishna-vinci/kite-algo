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
      className={cn("rounded-[1.5rem] border border-border/70 bg-card/70 p-4 backdrop-blur", className)}
      {...props}
    >
      {(eyebrow || title || action) && (
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            {eyebrow ? <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{eyebrow}</p> : null}
            {title ? <h3 className="mt-2 text-base font-semibold tracking-tight">{title}</h3> : null}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
