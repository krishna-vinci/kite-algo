/**
 * Typed wrappers over the hosted-strategy operator API (`/api/strategies/*`).
 *
 * One function per documented endpoint. Mutations rely on `apiFetch`
 * propagating the session cookie; the server enforces same-origin on unsafe
 * methods. Nothing here invents a route or hardcodes account choices.
 */

import { apiFetch } from "@/lib/api/client";
import type {
  CreateHostedStrategyPayload,
  CreateHostedVersionPayload,
  HostedJobDetail,
  HostedJobList,
  HostedStrategy,
  HostedStrategyList,
  HostedStrategyOptions,
  HostedVersion,
  HostedVersionList,
  JobLogs,
  ReconciliationAction,
  ReconciliationInspection,
  RunNotificationList,
  RunNowPayload,
  RunNowResponse,
  StopJobPayload,
  StopJobResponse,
  UpdateHostedStrategyPayload,
} from "@/lib/hosted-strategies/types";

const BASE = "/api/strategies";

export async function fetchHostedOptions(): Promise<HostedStrategyOptions> {
  return apiFetch<HostedStrategyOptions>(`${BASE}/options`);
}

export async function fetchHostedStrategies(): Promise<HostedStrategyList> {
  return apiFetch<HostedStrategyList>(BASE);
}

export async function fetchHostedStrategy(strategyId: string): Promise<HostedStrategy> {
  return apiFetch<HostedStrategy>(`${BASE}/${encodeURIComponent(strategyId)}`);
}

export async function createHostedStrategy(
  payload: CreateHostedStrategyPayload,
): Promise<HostedStrategy> {
  return apiFetch<HostedStrategy>(BASE, { method: "POST", json: payload });
}

export async function updateHostedStrategy(
  strategyId: string,
  payload: UpdateHostedStrategyPayload,
): Promise<HostedStrategy> {
  return apiFetch<HostedStrategy>(`${BASE}/${encodeURIComponent(strategyId)}`, {
    method: "PATCH",
    json: payload,
  });
}

export async function fetchHostedVersions(strategyId: string): Promise<HostedVersionList> {
  return apiFetch<HostedVersionList>(`${BASE}/${encodeURIComponent(strategyId)}/versions`);
}

export async function createHostedVersion(
  strategyId: string,
  payload: CreateHostedVersionPayload,
): Promise<HostedVersion> {
  return apiFetch<HostedVersion>(`${BASE}/${encodeURIComponent(strategyId)}/versions`, {
    method: "POST",
    json: payload,
  });
}

export async function fetchHostedJobs(strategyId: string): Promise<HostedJobList> {
  return apiFetch<HostedJobList>(`${BASE}/${encodeURIComponent(strategyId)}/jobs`);
}

export async function fetchHostedJob(strategyId: string, jobId: string): Promise<HostedJobDetail> {
  return apiFetch<HostedJobDetail>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}`,
  );
}

export async function runHostedStrategy(
  strategyId: string,
  payload: RunNowPayload,
): Promise<RunNowResponse> {
  return apiFetch<RunNowResponse>(`${BASE}/${encodeURIComponent(strategyId)}/jobs`, {
    method: "POST",
    json: payload,
  });
}

export async function stopHostedJob(
  strategyId: string,
  jobId: string,
  payload: StopJobPayload,
): Promise<StopJobResponse> {
  return apiFetch<StopJobResponse>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}/stop`,
    { method: "POST", json: payload },
  );
}

export async function fetchHostedJobLogs(
  strategyId: string,
  jobId: string,
  params?: { after_seq?: number; limit?: number },
): Promise<JobLogs> {
  const search = new URLSearchParams();
  if (params?.after_seq !== undefined) search.set("after_seq", String(params.after_seq));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return apiFetch<JobLogs>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}/logs${qs ? `?${qs}` : ""}`,
  );
}

export async function fetchHostedJobNotifications(
  strategyId: string,
  jobId: string,
): Promise<RunNotificationList> {
  return apiFetch<RunNotificationList>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}/notifications`,
  );
}

export async function inspectHostedReconciliation(
  strategyId: string,
  jobId: string,
): Promise<ReconciliationInspection> {
  return apiFetch<ReconciliationInspection>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}/reconciliation`,
  );
}

export async function reconcileHostedJob(
  strategyId: string,
  jobId: string,
  payload: { attempt: number; lease_epoch?: number },
): Promise<ReconciliationAction> {
  return apiFetch<ReconciliationAction>(
    `${BASE}/${encodeURIComponent(strategyId)}/jobs/${encodeURIComponent(jobId)}/reconciliation`,
    { method: "POST", json: payload },
  );
}
