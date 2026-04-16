"use client";

import { useTradingConsoleData } from "@/features/trading/hooks/use-trading-console-data";
import { TradingConsolePage } from "@/features/trading/components/trading-console-page";

export default function TradingPage() {
  const snapshot = useTradingConsoleData();
  return <TradingConsolePage snapshot={snapshot} />;
}
