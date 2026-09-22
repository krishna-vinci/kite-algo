/**
 * Types for the hosted-strategy operator API (`/api/strategies/*`).
 *
 * These mirror the backend response shapes (snake_case) exactly; nothing here
 * invents a field. Account scopes and modes come from the server `/options`
 * endpoint, never a hardcoded list.
 */

export type HostedStrategyOptions = {
  account_scopes: string[];
  execution_modes: string[];
  job_kinds: string[];
  stale_exit_policies: string[];
  hosted_execution_only: boolean;
  /**
   * Live lanes this deployment supports (`cnc`, `mis`, `futures`, `options`).
   * Empty when live is not enabled here; absent on servers that predate the
   * field, which is "unknown", never "none".
   */
  live_lanes?: string[];
  /**
   * Whether a live run still needs the owner's own plan approval. Servers that
   * predate the field are approval-gated too, so only `false` relaxes it.
   */
  live_requires_owner_approval?: boolean;
};

export type HostedStrategy = {
  strategy_id: string;
  owner_id: string;
  name: string;
  template_id: string;
  description: string | null;
  default_execution_mode: string;
  default_job_kind: string;
  default_account_scope: string;
  max_duration_s: number;
  progress_deadline_s: number;
  stale_exit_policy: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
};

export type HostedStrategyList = { strategies: HostedStrategy[] };

export type HostedVersion = {
  version_id: string;
  strategy_id: string;
  version: number;
  source: string;
  source_sha256: string;
  parameters_schema: Record<string, unknown>;
  capabilities_snapshot: Record<string, unknown>;
  created_by: string;
  created_at: string | null;
};

export type HostedVersionList = { versions: HostedVersion[] };

export type StopView = {
  requested: boolean;
  state: "none" | "requested" | "stopping" | "confirmed" | "cleanup_unresolved";
  requested_at: string | null;
  requested_by: string | null;
  replacement_blocked: boolean;
  note: string;
};

export type HostedJobSummary = {
  job_id: string;
  strategy_id: string;
  owner_id: string;
  attempt: number;
  status: string;
  desired_state: string;
  execution_mode: string;
  account_scope: string;
  run_id: string | null;
  replacement_blocked: boolean;
  recovery_required_at: string | null;
  reconciled_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type HostedJobDetail = HostedJobSummary & {
  handoff_at: string | null;
  process_cleanup_state: string | null;
  process_cleanup_at: string | null;
  process_cleanup_actor: string | null;
  last_progress_at: string | null;
  version_id: string;
  token_present: boolean;
  stop_requested_at: string | null;
  stop_requested_by: string | null;
  stop: StopView;
  logs_discarded: boolean;
  logs_source: string | null;
};

export type HostedJobList = { jobs: HostedJobSummary[] };

export type ReconciliationAssessment = {
  allowed: boolean;
  case: string;
  reason_code: string;
  blocking_reasons: string[];
  notes: string[];
};

export type ReconciliationAudit = {
  id: string;
  attempt: number;
  outcome: string;
  reason_code: string;
  actor_id: string;
  run_id: string | null;
  evidence: Record<string, unknown>;
  created_at: string | null;
};

export type ReconciliationInspection = {
  job_id: string;
  strategy_id: string;
  attempt: number;
  replacement_blocked: boolean;
  assessment: ReconciliationAssessment;
  evidence: Record<string, unknown>;
  history: ReconciliationAudit[];
};

export type ReconciliationAction = {
  status: string;
  job_id: string;
  attempt: number;
  case: string;
  reason_code: string;
  replacement_blocked: boolean;
  blocking_reasons: string[];
  evidence: Record<string, unknown>;
  audit_id: string;
};

export type JobLogEntry = { seq: number; content: string; created_at: string | null };

export type JobLogs = {
  job_id: string;
  available: boolean;
  truncated: boolean;
  source: string | null;
  next_seq: number;
  entries: JobLogEntry[];
  notice: string;
};

export type DeliveryAttempt = {
  attempt_no: number;
  outcome: string;
  detail: string;
  provider_id: string | null;
  created_at: string | null;
};

export type Delivery = {
  delivery_id: string;
  channel_id: string;
  channel_name: string | null;
  status: string;
  attempts: number;
  last_error: string | null;
  delivered_at: string | null;
  attempt_history: DeliveryAttempt[];
};

export type RunNotificationEvent = {
  event_id: string;
  run_id: string;
  fired_at: string | null;
  text: string;
  subject: string | null;
  deliveries: Delivery[];
  delivery_status_counts: Record<string, number>;
};

export type RunNotificationList = {
  job_id: string;
  run_id: string | null;
  events: RunNotificationEvent[];
};

export type CreateHostedStrategyPayload = {
  name: string;
  description?: string | null;
  execution_mode: string;
  job_kind: string;
  account_scope: string;
  max_duration_s: number;
  progress_deadline_s: number;
  stale_exit_policy: string;
};

export type UpdateHostedStrategyPayload = {
  description?: string | null;
  status?: string;
};

export type CreateHostedVersionPayload = {
  source: string;
  parameters_schema?: Record<string, unknown>;
  capabilities?: { data?: boolean; trade?: boolean; notify?: boolean };
};

export type RunNowPayload = {
  version_id: string;
  params: Record<string, unknown>;
  execution_mode?: string;
  job_kind?: string;
  idempotency_key: string;
};

export type RunNowResponse = { idempotent: boolean; job: HostedJobDetail };

export type StopJobPayload = { attempt: number; lease_epoch?: number };
export type StopJobResponse = {
  job_id: string;
  attempt: number;
  idempotent: boolean;
  stop: StopView;
};
