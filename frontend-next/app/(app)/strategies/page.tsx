"use client";

import { TradingConsolePage } from "@/features/trading/components/trading-console-page";
import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";

export default function StrategiesPage() {
  const snapshot = useTradingConsoleData();
  return <TradingConsolePage snapshot={snapshot} />;
}
