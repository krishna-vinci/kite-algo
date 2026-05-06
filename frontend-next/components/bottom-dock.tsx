"use client";

import { CompactTradingDock } from "@/features/trading/components/compact-trading-dock";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";

type BottomDockProps = Readonly<{
  workspace: string;
}>;

export function shouldShowBottomDock(workspace: string): boolean {
  return workspace.startsWith("/dashboard") || workspace.startsWith("/strategies");
}

export function BottomDock({ workspace }: BottomDockProps) {
  if (!shouldShowBottomDock(workspace)) {
    return null;
  }

  const snapshot = useTradingConsoleData();
  return <CompactTradingDock workspace={workspace} paper={snapshot.paper} broker={snapshot.broker} />;
}
