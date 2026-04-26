import { useQuery } from "@tanstack/react-query";

import { fetchControlPlaneSnapshot } from "@/features/trading/api";

export function useControlPlaneSnapshot() {
  return useQuery({
    queryKey: ["control-plane", "strategy-positions"],
    queryFn: fetchControlPlaneSnapshot,
    refetchInterval: 5_000,
  });
}
