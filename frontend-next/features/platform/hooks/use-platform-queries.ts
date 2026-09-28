"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchPlatformLiveSettings,
  fetchPlatformStatus,
  getOptionsSettings,
  updateOptionsSettings,
  updatePlatformLiveSettings,
} from "@/lib/platform/api";
import type { OptionsSettingsPayload } from "@/lib/platform/types";

export const platformKeys = {
  status: () => ["platform", "status"] as const,
  liveSettings: () => ["platform", "live-settings"] as const,
  optionsSettings: () => ["platform", "options-settings"] as const,
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

export function usePlatformOptionsSettings() {
  return useQuery({
    queryKey: platformKeys.optionsSettings(),
    queryFn: getOptionsSettings,
  });
}

export function useUpdatePlatformOptionsSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: OptionsSettingsPayload) => updateOptionsSettings(payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: platformKeys.optionsSettings() });
    },
  });
}
