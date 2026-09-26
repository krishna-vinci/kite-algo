"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchPlatformLiveSettings,
  fetchPlatformStatus,
  updatePlatformLiveSettings,
} from "@/lib/platform/api";

export const platformKeys = {
  status: () => ["platform", "status"] as const,
  liveSettings: () => ["platform", "live-settings"] as const,
};

/** Top-bar mode chip + status dots. Polls every 15s per the P2-UX contract. */
export function usePlatformStatus() {
  return useQuery({
    queryKey: platformKeys.status(),
    queryFn: fetchPlatformStatus,
    refetchInterval: 15_000,
  });
}

export function usePlatformLiveSettings() {
  return useQuery({
    queryKey: platformKeys.liveSettings(),
    queryFn: fetchPlatformLiveSettings,
  });
}

export function useUpdatePlatformLiveSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: updatePlatformLiveSettings,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: platformKeys.liveSettings() });
      void client.invalidateQueries({ queryKey: platformKeys.status() });
    },
  });
}
