"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { hostedKeys } from "@/features/strategies/hooks/keys";
import { anyJobStillMoving } from "@/features/strategies/lib/modes";
import {
  approveExecutionRequest,
  checkSourceReadiness,
  createHostedStrategy,
  createHostedVersion,
  fetchAdmissionPolicy,
  fetchAuthorization,
  fetchExecutionGrants,
  fetchExecutionRequests,
  fetchHostedJob,
  fetchHostedJobLogs,
  fetchHostedJobNotifications,
  fetchHostedJobs,
  fetchHostedOptions,
  fetchHostedPositions,
  fetchHostedSchedule,
  fetchHostedScheduleOccurrences,
  fetchHostedStrategies,
  fetchHostedStrategy,
  fetchHostedVersions,
  fetchOperatorCalendar,
  fetchOptionRun,
  fetchOptionRunRepair,
  fetchOptionRuns,
  fetchPlan,
  inspectHostedReconciliation,
  issueExecutionGrant,
  reconcileHostedJob,
  rejectExecutionRequest,
  revokeExecutionGrant,
  runHostedStrategy,
  saveAdmissionPolicy,
  saveHostedSchedule,
  setAuthorizationMode,
  setHostedScheduleEnabled,
  stopHostedJob,
  submitOptionRunRepair,
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
    // The list is what the operator lands on straight after "Create and run".
    // A job that was queued at first paint must keep refreshing until the server
    // reports its real terminal state, or the page keeps claiming "Queued" long
    // after the attempt finished. Poll only while something is actually moving.
    refetchInterval: (query) => (anyJobStillMoving(query.state.data?.jobs) ? 5_000 : false),
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

/**
 * Readiness is a request, not a query: the source is a form value, and the
 * composer decides when it is worth asking (debounced) and which answer is the
 * newest.
 */
export function useSourceReadiness() {
  return useMutation({ mutationFn: (source: string) => checkSourceReadiness(source) });
}

export function useAuthorization(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.authorization(strategyId ?? ""),
    queryFn: () => fetchAuthorization(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useExecutionGrants(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.grants(strategyId ?? ""),
    queryFn: () => fetchExecutionGrants(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useExecutionRequests(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.executionRequests(strategyId ?? ""),
    queryFn: () => fetchExecutionRequests(strategyId as string),
    enabled: Boolean(strategyId),
    // A request that is still moving (waiting, queued, dispatching) is worth
    // re-reading; anything terminal is not polled.
    refetchInterval: (query) => {
      const rows = query.state.data?.requests ?? [];
      const moving = rows.some((row) =>
        ["requested", "awaiting_approval", "queued", "dispatching"].includes(row.status),
      );
      return moving ? 5_000 : false;
    },
  });
}

export function useAdmissionPolicy(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.admissionPolicy(strategyId ?? ""),
    queryFn: () => fetchAdmissionPolicy(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useHostedSchedule(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.schedule(strategyId ?? ""),
    queryFn: () => fetchHostedSchedule(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useHostedScheduleOccurrences(strategyId: string | null, enabled = true) {
  return useQuery({
    queryKey: hostedKeys.scheduleOccurrences(strategyId ?? ""),
    queryFn: () => fetchHostedScheduleOccurrences(strategyId as string),
    enabled: Boolean(strategyId) && enabled,
  });
}

export function useOperatorCalendar(
  params: { exchange: string; segment: string; from: string; to: string } | null,
) {
  return useQuery({
    queryKey: hostedKeys.calendar(params?.exchange ?? "", params?.segment ?? ""),
    queryFn: () => fetchOperatorCalendar(params as NonNullable<typeof params>),
    enabled: Boolean(params),
  });
}

export function useHostedPlan(strategyId: string | null, proposalId: string | null) {
  return useQuery({
    queryKey: hostedKeys.plan(strategyId ?? "", proposalId ?? ""),
    queryFn: () => fetchPlan(strategyId as string, proposalId as string),
    enabled: Boolean(strategyId && proposalId),
  });
}

export function useHostedPositions(strategyId: string | null, environment: string) {
  return useQuery({
    queryKey: hostedKeys.positions(strategyId ?? "", environment),
    queryFn: () => fetchHostedPositions(strategyId as string, environment),
    enabled: Boolean(strategyId && environment),
  });
}

export function useSetAuthorizationMode(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof setAuthorizationMode>[1]) =>
      setAuthorizationMode(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.authorization(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.grants(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.strategy(strategyId) });
    },
  });
}

export function useIssueExecutionGrant(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof issueExecutionGrant>[1]) =>
      issueExecutionGrant(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.authorization(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.grants(strategyId) });
    },
  });
}

export function useRevokeExecutionGrant(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof revokeExecutionGrant>[1]) =>
      revokeExecutionGrant(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.authorization(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.grants(strategyId) });
    },
  });
}

export function useExecutionRequestDecision(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { requestId: string; decision: "approve" | "reject"; reason?: string }) =>
      input.decision === "approve"
        ? approveExecutionRequest(strategyId, input.requestId, { reason: input.reason ?? null })
        : rejectExecutionRequest(strategyId, input.requestId, { reason: input.reason ?? null }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.executionRequests(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.jobs(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.authorization(strategyId) });
    },
  });
}

export function useSaveAdmissionPolicy(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof saveAdmissionPolicy>[1]) =>
      saveAdmissionPolicy(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.admissionPolicy(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.authorization(strategyId) });
    },
  });
}

export function useSaveHostedSchedule(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof saveHostedSchedule>[1]) =>
      saveHostedSchedule(strategyId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.schedule(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.scheduleOccurrences(strategyId) });
    },
  });
}

export function useSetHostedScheduleEnabled(strategyId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) => setHostedScheduleEnabled(strategyId, enabled),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.schedule(strategyId) });
      void client.invalidateQueries({ queryKey: hostedKeys.scheduleOccurrences(strategyId) });
    },
  });
}

// ---------------------------------------------------------------------------
// B2.6a: owner-facing option-run operations
// ---------------------------------------------------------------------------

export function useOptionRuns(strategyId: string | null) {
  return useQuery({
    queryKey: hostedKeys.optionRuns(strategyId ?? ""),
    queryFn: () => fetchOptionRuns(strategyId as string),
    enabled: Boolean(strategyId),
  });
}

export function useOptionRun(strategyId: string | null, optionRunId: string | null) {
  return useQuery({
    queryKey: hostedKeys.optionRun(strategyId ?? "", optionRunId ?? ""),
    queryFn: () => fetchOptionRun(strategyId as string, optionRunId as string),
    enabled: Boolean(strategyId && optionRunId),
  });
}

export function useOptionRunRepairAssessment(strategyId: string | null, optionRunId: string | null) {
  return useQuery({
    queryKey: hostedKeys.optionRunRepair(strategyId ?? "", optionRunId ?? ""),
    queryFn: () => fetchOptionRunRepair(strategyId as string, optionRunId as string),
    enabled: Boolean(strategyId && optionRunId),
  });
}

export function useSubmitOptionRunRepair(strategyId: string, optionRunId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (payload: Parameters<typeof submitOptionRunRepair>[2]) =>
      submitOptionRunRepair(strategyId, optionRunId, payload),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hostedKeys.optionRunRepair(strategyId, optionRunId) });
      void client.invalidateQueries({ queryKey: hostedKeys.optionRun(strategyId, optionRunId) });
      void client.invalidateQueries({ queryKey: hostedKeys.optionRuns(strategyId) });
    },
  });
}
