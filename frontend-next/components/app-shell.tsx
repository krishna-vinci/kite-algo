import { BottomDock } from "@/components/bottom-dock";
import { LeftRail } from "@/components/left-rail";
import { TopBar } from "@/components/top-bar";
import type { NavigationItem } from "@/lib/navigation";
import type { ReactNode } from "react";

type AppShellProps = Readonly<{
  navigation: NavigationItem[];
  activeHref: string;
  children: ReactNode;
}>;

export function AppShell({ navigation, activeHref, children }: AppShellProps) {
  const activeItem = navigation.find(
    (item) => activeHref === item.href || (item.href !== "/dashboard" && activeHref.startsWith(item.href)),
  );
  const hideGlobalDock = activeHref.startsWith("/options");

  return (
    <div className="grid min-h-screen grid-cols-[48px_1fr] grid-rows-[40px_1fr_auto] bg-[var(--bg)] text-[var(--text)]">
      <LeftRail navigation={navigation} activeHref={activeHref} />
      <TopBar title={activeItem?.label ?? "Dashboard"} />
      <main className="min-w-0 overflow-auto p-3">{children}</main>
      {hideGlobalDock ? <div /> : <BottomDock workspace={activeHref} />}
    </div>
  );
}
