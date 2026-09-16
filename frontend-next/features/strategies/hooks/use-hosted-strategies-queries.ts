"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { hostedKeys } from "@/features/strategies/hooks/keys";
import {
  createHostedStrategy,
  createHostedVersion,
  fetchHostedJob,
  fetchHostedJobLogs,
  fetchHostedJobNotifications,
  fetchHostedJobs,
  fetchHostedOptions,
  fetchHostedStrategies,
  fetchHostedStrategy,
  fetchHostedVersions,
  inspectHostedReconciliation,
  reconcileHostedJob,
  runHostedStrategy,
  stopHostedJob,
  updateHostedStrategy,
} from "@/lib/hosted-strategies/api";

/**
 * Server-authorized selection options (account scopes, modes, kinds, policies).
 * Cached for the session; the browser never hardcodes account choices.
 */
export function useHostedOptions() {
  return useQuery({
    queryKey: hostedKeys.options(),
    queryFn: fetchHostedOptions,
    staleTime: 5 * 60_000,
  });
}

export function useHostedStrategies() {
  return useQuery({ queryKey: hostedKeys.strategies(), queryFn: fetchHostedStrategies });
}

export function useHostedStrategy(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.strategy(strategyId ?? ""),
    queryFn: () => fetchHostedStrategy(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useHostedVersions(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.versions(strategyId ?? ""),
    queryFn: () => fetchHostedVersions(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useHostedJobs(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.jobs(strategyId ?? ""),
    queryFn: () => fetchHostedJobs(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useHostedJob(strategyId: string | null, jobId: string | null) {
  return useQuery({
    queryKey: hostedKeys.job(strategyId ?? "", jobId ?? ""),
    queryFn: () => fetchHostedJob(strategyId as string, jobId as string),
    enabled: Boolean(strategyId && jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      // Poll while the attempt is still moving; stop once it is terminal-ish.
      return status && ["queued", "starting", "running", "fencing"].includes(status) ? 5_000 : false;
    },
  });
}

/**
 * One query per requested page offset. Keeping the pages as separate queries is
 * what lets "Load more" append instead of replacing what the operator already
 * read (and avoids setState-in-effect accumulation).
 */
export function useHostedJobLogPages(strategyId: string | null, jobId: string | null, offsets: number[]) {
  return useQueries({
    queries: offsets.map((offset) => ({
      queryKey: [...hostedKeys.logs(strategyId ?? "", jobId ?? ""), offset, 200],
      queryFn: () =>
        fetchHostedJobLogs(strategyId as string, jobId as string, { after_seq: offset, limit: 200 }),
      enabled: Boolean(strategyId && jobId),
      refetchInterval: 5_000,
    })),
  });
}

export function useHostedJobNotifications(strategyId: string | null, jobId: string | null) {
  return useQuery({
    queryKey: hostedKeys.notifications(strategyId ?? "", jobId ?? ""),
    queryFn: () => fetchHostedJobNotifications(strategyId as string, jobId as string),
    enabled: Boolean(strategyId && jobId),
  });
}

export function useHostedReconciliation(strategyId: string | null, jobId: string | null) {
  return useQuery({
    queryKey: hostedKeys.reconciliation(strategyId ?? "", jobId ?? ""),
    queryFn: () => inspectHostedReconciliation(strategyId as string, jobId as string),
    enabled: Boolean(strategyId && jobId),
  });
}

export function useCreateHostedStrategy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createHostedStrategy,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
    },
  });
}

export function useUpdateHostedStrategy(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof updateHostedStrategy>[1]) =>
      updateHostedStrategy(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.strategy(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
    },
  });
}

export function useCreateHostedVersion(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof createHostedVersion>[1]) =>
      createHostedVersion(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.versions(strategyId) });
    },
  });
}

export function useRunHostedStrategy(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof runHostedStrategy>[1]) =>
      runHostedStrategy(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.jobs(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.strategy(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.strategies() });
    },
  });
}

export function useStopHostedJob(strategyId: string, jobId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof stopHostedJob>[2]) =>
      stopHostedJob(strategyId, jobId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.job(strategyId, jobId) });
      void client.invalidateQueries({ queryKey: hostedKeys.jobs(strategyId) });
    },
  });
}

export function useReconcileHostedJob(strategyId: string, jobId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof reconcileHostedJob>[2]) =>
      reconcileHostedJob(strategyId, jobId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.job(strategyId, jobId) });
      void client.invalidateQueries({ queryKey: hostedKeys.jobs(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.reconciliation(strategyId, jobId) });
    },
  });
}
