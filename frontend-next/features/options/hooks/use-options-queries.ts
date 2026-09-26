"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { optionsKeys } from "@/features/options/hooks/keys";
import {
  fetchOptionChain,
  fetchOptionExpiries,
  fetchOptionMaxPain,
  fetchOptionPcr,
  fetchOptionSession,
  startOptionSessions,
} from "@/lib/options/api";

const CHAIN_POLL_MS = 5_000;

/** `null` data means no active session yet — the page starts one. */
export function useOptionSession(underlying: string) {
  return useQuery({
    queryKey: optionsKeys.session(underlying),
    queryFn: () => fetchOptionSession(underlying),
    enabled: Boolean(underlying),
  });
}

export function useOptionExpiries(underlying: string, enabled: boolean) {
  return useQuery({
    queryKey: optionsKeys.expiries(underlying),
    queryFn: () => fetchOptionExpiries(underlying),
    enabled: enabled && Boolean(underlying),
  });
}

export function useStartOptionSession(underlying: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => startOptionSessions({ items: [{ underlying }] }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: optionsKeys.session(underlying) });
      void queryClient.invalidateQueries({ queryKey: optionsKeys.expiries(underlying) });
    },
  });
}

export function useOptionChain(underlying: string, expiry: string | null) {
  return useQuery({
    queryKey: optionsKeys.chain(underlying, expiry ?? ""),
    queryFn: () => fetchOptionChain(underlying, expiry as string),
    enabled: Boolean(underlying) && Boolean(expiry),
    refetchInterval: CHAIN_POLL_MS,
  });
}

export function useOptionPcr(underlying: string, expiry: string | null) {
  return useQuery({
    queryKey: optionsKeys.pcr(underlying, expiry ?? ""),
    queryFn: () => fetchOptionPcr(underlying, expiry as string),
    enabled: Boolean(underlying) && Boolean(expiry),
    refetchInterval: CHAIN_POLL_MS,
  });
}

export function useOptionMaxPain(underlying: string, expiry: string | null) {
  return useQuery({
    queryKey: optionsKeys.maxPain(underlying, expiry ?? ""),
    queryFn: () => fetchOptionMaxPain(underlying, expiry as string),
    enabled: Boolean(underlying) && Boolean(expiry),
    refetchInterval: CHAIN_POLL_MS,
  });
}
