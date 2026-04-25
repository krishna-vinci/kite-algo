import { useQuery } from "@tanstack/react-query";

import { fetchPaperStrategySummary } from "@/features/trading/api";

export function usePaperStrategySummary(accountScope = "default") {
  return useQuery({
    queryKey: ["trading", "paper-summary", accountScope],
    queryFn: () => fetchPaperStrategySummary(accountScope),
    refetchInterval: 4_000,
  });
}
