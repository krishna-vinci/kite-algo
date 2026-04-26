import { useMemo } from "react";

import type { TradingConsoleSnapshot } from "@/features/trading/types";
import { useControlPlaneSnapshot } from "@/features/trading/hooks/use-control-plane-snapshot";
import { useLiveBrokerPositions } from "@/features/trading/hooks/use-live-broker-positions";
import { useMarketwatchQuotes } from "@/features/trading/hooks/use-marketwatch-quotes";
import { usePaperStrategySummary } from "@/features/trading/hooks/use-paper-strategy-summary";
import { useRuntimeStatusQuery } from "@/features/trading/hooks/use-runtime-status-query";

const FALLBACK_RUNTIME: TradingConsoleSnapshot["runtime"] = {
  brokerConnected: false,
  brokerStatus: "unknown",
  brokerMode: "system",
  brokerLastSuccessAt: null,
  brokerLastFailureAt: null,
  brokerLastError: null,
  brokerNextRefreshAt: null,
  websocketStatus: "unknown",
  paperAvailable: false,
  appAuthenticated: false,
};

export function useTradingConsoleData(): TradingConsoleSnapshot {
  const runtimeQuery = useRuntimeStatusQuery();
  const paperQuery = usePaperStrategySummary();
  const brokerQuery = useLiveBrokerPositions();
  const controlQuery = useControlPlaneSnapshot();
  const market = useMarketwatchQuotes();

  return useMemo(
    () => ({
      runtime: runtimeQuery.data ?? FALLBACK_RUNTIME,
      quotes: market.quotes,
      paper: paperQuery.data ?? {
        accountScope: "default",
        account: {
          accountScope: "default",
          currency: "INR",
          startingBalance: 0,
          availableFunds: 0,
          blockedFunds: 0,
          realizedPnl: 0,
          unrealizedPnl: 0,
          openPositionCount: 0,
        },
        activeStrategyCount: 0,
        strategies: [],
      },
      broker: brokerQuery.data ?? { positions: [], activeCount: 0 },
      control: controlQuery.data ?? null,
    }),
    [brokerQuery.data, controlQuery.data, market.quotes, paperQuery.data, runtimeQuery.data],
  );
}
