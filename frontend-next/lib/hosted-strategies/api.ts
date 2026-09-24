/**
 * Typed wrappers over the hosted-strategy operator API (`/api/strategies/*`).
 *
 * One function per documented endpoint. Mutations rely on `apiFetch`
 * propagating the session cookie; the server enforces same-origin on unsafe
 * methods. Nothing here invents a route or hardcodes account choices.
 */

import { ApiClientError, apiFetch } from "@/lib/api/client";
import type {
  CreateHostedStrategyPayload,
  CreateHostedVersionPayload,
  AdmissionPolicy,
  AdmissionPolicyPayload,
  AdmissionVerdict,
  ApprovalRow,
  AuthorizationModeResponse,
  AuthorizationStatus,
  CalendarSessions,
  ExecutionGrant,
  ExecutionGrantRevokeResponse,
  ExecutionRequestDecisionResponse,
  ExecutionRequestList,
  ExecutionRequestRow,
  HostedPositionList,
  HostedJobDetail,
  HostedJobList,
  HostedSchedule,
  HostedScheduleOccurrence,
  HostedSchedulePayload,
  HostedStrategy,
  HostedStrategyList,
  HostedStrategyOptions,
  HostedVersion,
  HostedVersionList,
  JobLogs,
  PlanDetail,
  ReconciliationAction,
  ReconciliationInspection,
  ReservationRow,
  RunNotificationList,
  RunNowPayload,
  RunNowResponse,
  SourceReadiness,
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

/**
 * First-run readiness for a source file. Parses with `ast` server-side; the
 * browser never executes the uploaded code and no row is written.
 */
export async function checkSourceReadiness(source: string): Promise<SourceReadiness> {
  return apiFetch<SourceReadiness>(`${BASE}/readiness`, { method: "POST", json: { source } });
}

export async function fetchAuthorization(strategyId: string): Promise<AuthorizationStatus> {
  return apiFetch<AuthorizationStatus>(`${BASE}/${encodeURIComponent(strategyId)}/authorization`);
}

export async function setAuthorizationMode(
  strategyId: string,
  payload: { mode: "approval_based" | "autonomous"; reason?: string | null },
): Promise<AuthorizationModeResponse> {
  return apiFetch<AuthorizationModeResponse>(
    `${BASE}/${encodeURIComponent(strategyId)}/authorization`,
    { method: "PUT", json: payload },
  );
}

export async function fetchExecutionGrants(strategyId: string): Promise<ExecutionGrant[]> {
  return apiFetch<ExecutionGrant[]>(
    `${BASE}/${encodeURIComponent(strategyId)}/authorization/grants`,
  );
}

/**
 * Issue the owner's standing authorization. The version's source hash, the
 * account and the policy hash are derived server-side; only the environment is
 * chosen. The idempotency key is a technical identity and is never rendered.
 */
export async function issueExecutionGrant(
  strategyId: string,
  payload: { idempotency_key: string; version_id: string; execution_environment: string; expires_at?: string | null },
): Promise<ExecutionGrant> {
  return apiFetch<ExecutionGrant>(
    `${BASE}/${encodeURIComponent(strategyId)}/authorization/grants`,
    { method: "POST", json: payload },
  );
}

export async function revokeExecutionGrant(
  strategyId: string,
  payload: { grant_id?: string | null; reason?: string | null },
): Promise<ExecutionGrantRevokeResponse> {
  return apiFetch<ExecutionGrantRevokeResponse>(
    `${BASE}/${encodeURIComponent(strategyId)}/authorization/grants/revoke`,
    { method: "POST", json: payload },
  );
}

export async function fetchExecutionRequests(strategyId: string): Promise<ExecutionRequestList> {
  return apiFetch<ExecutionRequestList>(
    `${BASE}/${encodeURIComponent(strategyId)}/execution-requests`,
  );
}

export async function approveExecutionRequest(
  strategyId: string,
  requestId: string,
  payload: { reason?: string | null } = {},
): Promise<ExecutionRequestDecisionResponse> {
  return apiFetch<ExecutionRequestDecisionResponse>(
    `${BASE}/${encodeURIComponent(strategyId)}/execution-requests/${encodeURIComponent(requestId)}/approve`,
    { method: "POST", json: payload },
  );
}

export async function rejectExecutionRequest(
  strategyId: string,
  requestId: string,
  payload: { reason?: string | null } = {},
): Promise<ExecutionRequestDecisionResponse> {
  return apiFetch<ExecutionRequestDecisionResponse>(
    `${BASE}/${encodeURIComponent(strategyId)}/execution-requests/${encodeURIComponent(requestId)}/reject`,
    { method: "POST", json: payload },
  );
}

export async function fetchExecutionRequest(
  strategyId: string,
  requestId: string,
): Promise<ExecutionRequestRow> {
  return apiFetch<ExecutionRequestRow>(
    `${BASE}/${encodeURIComponent(strategyId)}/execution-requests/${encodeURIComponent(requestId)}`,
  );
}

export async function fetchAdmissionPolicy(strategyId: string): Promise<AdmissionPolicy | null> {
  try {
    return await apiFetch<AdmissionPolicy>(
      `${BASE}/${encodeURIComponent(strategyId)}/admission-policy`,
    );
  } catch (error) {
    if (error instanceof ApiClientError && error.status === 404) return null;
    throw error;
  }
}

export async function saveAdmissionPolicy(
  strategyId: string,
  payload: AdmissionPolicyPayload,
): Promise<AdmissionPolicy> {
  return apiFetch<AdmissionPolicy>(`${BASE}/${encodeURIComponent(strategyId)}/admission-policy`, {
    method: "PUT",
    json: payload,
  });
}

export async function fetchHostedSchedule(strategyId: string): Promise<HostedSchedule | null> {
  return apiFetch<HostedSchedule | null>(`${BASE}/${encodeURIComponent(strategyId)}/schedule`);
}

export async function saveHostedSchedule(
  strategyId: string,
  payload: HostedSchedulePayload,
): Promise<HostedSchedule> {
  return apiFetch<HostedSchedule>(`${BASE}/${encodeURIComponent(strategyId)}/schedule`, {
    method: "PUT",
    json: payload,
  });
}

export async function setHostedScheduleEnabled(
  strategyId: string,
  enabled: boolean,
): Promise<HostedSchedule> {
  return apiFetch<HostedSchedule>(`${BASE}/${encodeURIComponent(strategyId)}/schedule/enabled`, {
    method: "POST",
    json: { enabled },
  });
}

export async function fetchHostedScheduleOccurrences(
  strategyId: string,
): Promise<HostedScheduleOccurrence[]> {
  return apiFetch<HostedScheduleOccurrence[]>(
    `${BASE}/${encodeURIComponent(strategyId)}/schedule/occurrences`,
  );
}

/**
 * Exchange sessions for the schedule editor. Exchange and segment are explicit
 * so an MCX or currency schedule is never shown NSE/CM timing.
 */
export async function fetchOperatorCalendar(params: {
  exchange: string;
  segment: string;
  from: string;
  to: string;
}): Promise<CalendarSessions> {
  const search = new URLSearchParams({
    exchange: params.exchange,
    segment: params.segment,
    from: params.from,
    to: params.to,
  });
  return apiFetch<CalendarSessions>(`${BASE}/calendar?${search.toString()}`);
}

/**
 * One frozen plan, by its own id. A durable execution request records the plan
 * id, so this is the lookup the operator's plan review uses.
 */
export async function fetchPlan(strategyId: string, planId: string): Promise<PlanDetail> {
  return apiFetch<PlanDetail>(
    `${BASE}/${encodeURIComponent(strategyId)}/plans/by-id/${encodeURIComponent(planId)}`,
  );
}

export async function previewPlanAdmission(
  strategyId: string,
  planId: string,
): Promise<AdmissionVerdict> {
  return apiFetch<AdmissionVerdict>(
    `${BASE}/${encodeURIComponent(strategyId)}/plans/${encodeURIComponent(planId)}/admission`,
    { method: "POST", json: {} },
  );
}

export async function fetchReservations(strategyId: string): Promise<ReservationRow[]> {
  const body = await apiFetch<{ reservations: ReservationRow[] }>(
    `${BASE}/${encodeURIComponent(strategyId)}/reservations`,
  );
  return body.reservations;
}

export async function fetchApprovals(strategyId: string): Promise<ApprovalRow[]> {
  const body = await apiFetch<{ approvals: ApprovalRow[] }>(
    `${BASE}/${encodeURIComponent(strategyId)}/approvals`,
  );
  return body.approvals;
}

export async function fetchHostedPositions(
  strategyId: string,
  environment: string,
): Promise<HostedPositionList> {
  const search = new URLSearchParams({ environment });
  return apiFetch<HostedPositionList>(
    `${BASE}/${encodeURIComponent(strategyId)}/positions?${search.toString()}`,
  );
}
