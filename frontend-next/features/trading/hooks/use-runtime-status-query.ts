import { useQuery } from "@tanstack/react-query";

import { fetchTradingRuntimeStatus } from "@/features/trading/api";

export function useRuntimeStatusQuery() {
  return useQuery({
    queryKey: ["trading", "runtime-status"],
    queryFn: fetchTradingRuntimeStatus,
    refetchInterval: 30_000,
  });
}
