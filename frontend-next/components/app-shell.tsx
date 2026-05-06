import { BottomDock, shouldShowBottomDock } from "@/components/bottom-dock";
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
  const showBottomDock = shouldShowBottomDock(activeHref);

  return (
    <div
      className="grid min-h-screen grid-cols-[68px_1fr] bg-[var(--bg)] text-[var(--text)]"
      style={{ gridTemplateRows: showBottomDock ? "52px 1fr auto" : "52px 1fr" }}
    >
      <LeftRail navigation={navigation} activeHref={activeHref} />
      <TopBar title={activeItem?.label ?? "Dashboard"} />
      <main className="min-w-0 overflow-auto p-4 lg:p-5">{children}</main>
      {showBottomDock ? <BottomDock workspace={activeHref} /> : null}
    </div>
  );
}
