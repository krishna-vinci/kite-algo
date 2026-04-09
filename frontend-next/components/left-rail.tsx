import Link from "next/link";
import { cn } from "@/lib/utils";
import type { NavigationItem } from "@/lib/navigation";

type LeftRailProps = Readonly<{
  navigation: NavigationItem[];
  activeHref: string;
}>;

export function LeftRail({ navigation, activeHref }: LeftRailProps) {
  return (
    <aside className="row-span-3 flex h-screen w-12 flex-col items-center border-r border-[var(--border)] bg-[#0c0d12] py-2">
      <div className="mb-3 flex h-[30px] w-[30px] items-center justify-center rounded-md bg-[var(--accent)] text-[13px] font-extrabold text-white">
        K
      </div>

      <nav aria-label="Primary" className="flex flex-1 flex-col items-center gap-1">
        {navigation.map((item) => {
          const active = activeHref === item.href || (item.href !== "/dashboard" && activeHref.startsWith(item.href));

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              title={item.label}
              className={cn(
                "flex h-9 w-9 items-center justify-center rounded-md text-[10px] font-semibold transition-colors",
                active
                  ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                  : "text-[var(--dim)] hover:bg-white/5 hover:text-[var(--muted)]",
              )}
            >
              {item.short}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
