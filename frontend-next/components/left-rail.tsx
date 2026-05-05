import Link from "next/link";
import {
  Activity,
  BarChart2,
  BookOpen,
  LayoutDashboard,
  LayoutGrid,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { NavigationItem } from "@/lib/navigation";

type LeftRailProps = Readonly<{
  navigation: NavigationItem[];
  activeHref: string;
}>;

const iconByHref: Record<string, LucideIcon> = {
  "/dashboard": LayoutDashboard,
  "/strategies": Activity,
  "/journal": BookOpen,
  "/analytics": BarChart2,
  "/settings": Settings,
};

export function LeftRail({ navigation, activeHref }: LeftRailProps) {
  return (
    <aside className="row-span-3 flex h-screen w-[68px] flex-col items-center border-r border-[var(--border)] bg-[#0b0d13] px-2 py-3">
      <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--accent)] text-sm font-extrabold text-white shadow-[0_12px_24px_rgba(249,115,22,0.24)]">
        K
      </div>

      <nav aria-label="Primary" className="flex w-full flex-1 flex-col gap-1.5">
        {navigation.map((item) => {
          const active = activeHref === item.href || (item.href !== "/dashboard" && activeHref.startsWith(item.href));
          const Icon = iconByHref[item.href] ?? LayoutGrid;

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              title={item.label}
              aria-label={item.label}
              className={cn(
                "group relative flex min-h-11 w-full items-center justify-center rounded-xl border px-1.5 py-1 text-center transition-colors focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-1 focus-visible:ring-offset-[#0b0d13]",
                active
                  ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)] shadow-[0_10px_22px_rgba(249,115,22,0.08)]"
                  : "border-transparent text-[var(--dim)] hover:border-[var(--border)] hover:bg-white/5 hover:text-[var(--text)]",
              )}
            >
              <Icon className="h-[17px] w-[17px]" strokeWidth={2} />
              {item.tag ? <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-[var(--accent)]" /> : null}
              <span className="sr-only">{item.label}</span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
