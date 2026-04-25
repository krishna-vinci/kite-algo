"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const tabs = [
  { label: "Overview", href: "/journal" },
  { label: "Calendar", href: "/journal/calendar" },
  { label: "Trades", href: "/journal/trades" },
  { label: "Strategies", href: "/journal/strategies" },
  { label: "Rules", href: "/journal/rules" },
  { label: "Insights", href: "/journal/insights" },
] as const;

export function JournalNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Journal sections" className="flex items-center gap-1 overflow-x-auto">
      {tabs.map((tab) => {
        const active = pathname === tab.href;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "rounded-full border px-3 py-1.5 text-[11px] font-medium uppercase tracking-[0.24em] transition-colors whitespace-nowrap",
              active
                ? "border-primary/40 bg-primary/10 text-primary"
                : "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/25 hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
