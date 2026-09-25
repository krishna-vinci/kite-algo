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
  /**
   * `approval_based` (default) or `autonomous`. A server that predates the
   * field means approval-based, so only an explicit value is read.
   */
  authorization_mode?: string;
  /** Canonical product status; the hosted scheduling status is `status`. */
  product_status?: string;
  /** Which compute adapters this product has ("hosted" / "external"). */
  adapter_kinds?: string[];
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

// ---------------------------------------------------------------------------
// Phase 1: first-run source readiness (`POST /api/strategies/readiness`)
// ---------------------------------------------------------------------------

export type RunnerPackage = {
  import_name: string;
  distribution: string;
  extra?: string | null;
};

export type RunnerProfile = {
  id: string;
  python: string;
  base_image: string;
  packages: RunnerPackage[];
  server_side_indicators: boolean;
  runtime_pip_install: boolean;
  notes?: string | null;
};

/** `unknown` is a real answer: a check that cannot be proven is not a pass. */
export type ReadinessStatus = "ok" | "blocked" | "unknown";

export type ReadinessCheck = {
  id: string;
  status: ReadinessStatus;
  detail: string;
  remediation?: string | null;
};

export type SourceEntrypoint = {
  found: boolean;
  compatible: boolean;
  name?: string | null;
  is_async?: boolean | null;
  detail: string;
  remediation?: string | null;
};

export type SourceImports = {
  available: string[];
  missing: string[];
  optional_available: string[];
  optional_missing: string[];
  providers: Record<string, string>;
  /** `importlib`/`__import__`/`exec`/`eval` seen: cannot be certified ready. */
  dynamic: boolean;
};

export type SourceReadiness = {
  schema_version: number;
  status: "ready" | "blocked";
  profile: RunnerProfile;
  checks: ReadinessCheck[];
  entrypoint: SourceEntrypoint;
  imports: SourceImports;
  messages: string[];
};

// ---------------------------------------------------------------------------
// Phase 2: authorization mode, grants and durable execution requests
// ---------------------------------------------------------------------------

export type AdmissionPolicy = {
  strategy_id: string;
  account_id: string;
  allocation_inr: number | null;
  per_instrument_notional_inr: number | null;
  gross_notional_inr: number | null;
  max_open_instruments: number | null;
  admissions_per_window: number | null;
  admission_window_seconds: number | null;
  daily_loss_budget_inr: number | null;
  updated_by: string;
};

export type AdmissionPolicyPayload = {
  allocation_inr?: number | null;
  per_instrument_notional_inr?: number | null;
  gross_notional_inr?: number | null;
  max_open_instruments?: number | null;
  admissions_per_window?: number | null;
  admission_window_seconds?: number | null;
  daily_loss_budget_inr?: number | null;
};

/** Half the grant's policy basis: the owner's recorded admission policy. */
export type AdmissionEvidence = {
  account_id: string;
  allocation_inr: number | null;
  per_instrument_notional_inr: number | null;
  gross_notional_inr: number | null;
  max_open_instruments: number | null;
  admissions_per_window: number | null;
  admission_window_seconds: number | null;
  daily_loss_budget_inr: number | null;
};

export type ProtectionPolicy = {
  stale_exit_policy: string;
  max_duration_s: number;
  progress_deadline_s: number;
};

/** The exact policy basis a grant binds: admission + mandatory protection. */
export type PolicySnapshot = {
  admission: AdmissionEvidence | null;
  protection: ProtectionPolicy;
};

export type ExecutionGrant = {
  grant_id: string;
  owner_id: string;
  strategy_id: string;
  canonical_strategy_id: string;
  version_id: string;
  version_number: number;
  source_sha256: string;
  account_id: string;
  execution_environment: string;
  policy_hash: string;
  policy_snapshot: PolicySnapshot;
  issued_by: string;
  issued_at: string | null;
  expires_at: string | null;
  status: string;
  revoked_by: string | null;
  revoked_at: string | null;
  revocation_reason: string | null;
  superseded_by: string | null;
  superseded_at: string | null;
  supersession_reason: string | null;
  /** Technical. Hidden in the UI and never reused for a changed request. */
  request_key: string;
  content_sha256: string;
  created_at: string | null;
  idempotent: boolean;
};

export type AuthorizationStatus = {
  strategy_id: string;
  authorization_mode: string;
  active_grant: ExecutionGrant | null;
  policy_snapshot: PolicySnapshot;
  policy_hash: string;
  /** `false` means no capital basis is recorded: ask for the missing limits. */
  policy_concrete: boolean;
  grant_usable: boolean;
  blocking_reasons: string[];
  evaluated_at: string | null;
};

export type AuthorizationModeResponse = {
  strategy_id: string;
  authorization_mode: string;
  previous_mode: string;
  changed: boolean;
};

export type ExecutionGrantRevokeResponse = {
  grant: ExecutionGrant;
  revoked_at: string | null;
};

/** The executor's own word. `terminal` means "not dispatched again". */
export type ExecutionOutcomeState =
  | "submitted"
  | "accepted"
  | "filled"
  | "partial"
  | "rejected"
  | "no_op"
  | "uncertain"
  | "failed"
  | string;

export type ExecutionRequestRow = {
  request_id: string;
  owner_id: string;
  strategy_id: string;
  canonical_strategy_id: string;
  account_id: string;
  execution_environment: string;
  strategy_run_id: string;
  job_id: string | null;
  token_id: string | null;
  attempt: number | null;
  lease_epoch: number | null;
  version_id: string;
  version_number: number | null;
  source_sha256: string;
  policy_hash: string;
  evaluation_id: string | null;
  plan_id: string;
  plan_hash: string;
  authorization_mode: string;
  grant_id: string | null;
  status: string;
  refusal_code: string | null;
  refusal_detail: Record<string, unknown>;
  decision_kind: string | null;
  decision_actor: string | null;
  decision_at: string | null;
  decision_evidence: Record<string, unknown>;
  approval_id: string | null;
  reservation_id: string | null;
  execution_detail: Record<string, unknown>;
  outcome_state: ExecutionOutcomeState | null;
  dispatch_claim_id: string | null;
  dispatch_claimed_at: string | null;
  dispatch_started_at: string | null;
  dispatch_finished_at: string | null;
  /** Technical. Hidden in the UI. */
  idempotency_key: string;
  created_at: string | null;
  updated_at: string | null;
  terminal: boolean;
  executable: boolean;
};

export type ExecutionRequestList = {
  strategy_id: string;
  requests: ExecutionRequestRow[];
};

export type ExecutionRequestDecisionResponse = {
  request: ExecutionRequestRow;
  approved: boolean;
  rejected: boolean;
};

// ---------------------------------------------------------------------------
// Phase 4: schedules (operator create/edit/disable)
// ---------------------------------------------------------------------------

export type ScheduleKind = "daily" | "weekly" | "monthly" | "calendar";

export type HostedScheduleOccurrence = {
  occurrence_key: string;
  due_at: string | null;
  status: "pending" | "fired" | "skipped" | "expired" | string;
  fired_at: string | null;
  evaluation_id: string | null;
  skip_reason: string | null;
  detail: Record<string, unknown>;
};

export type HostedSchedule = {
  schedule_id: string;
  strategy_id: string;
  version_id: string;
  version_number: number | null;
  account_scope: string;
  execution_mode: string;
  job_kind: string;
  params_snapshot: Record<string, unknown>;
  schedule_kind: ScheduleKind | string;
  at_time: string;
  weekday: number | null;
  day_of_month: number | null;
  calendar_dates: string[];
  timezone: string;
  window_end: string | null;
  squareoff_at: string | null;
  enabled: boolean;
  manually_paused: boolean;
  max_duration_s: number;
  progress_deadline_s: number;
  misfire_grace_seconds: number;
  /** The runtime's own policy (`defer_until_resolved`), reported verbatim. */
  overlap_policy: string;
  next_occurrence_at: string | null;
  next_occurrence_key: string | null;
  last_occurrence: HostedScheduleOccurrence | null;
  created_at: string | null;
  updated_at: string | null;
};

export type HostedSchedulePayload = {
  version_id: string;
  execution_mode: string;
  job_kind: string;
  params: Record<string, unknown>;
  schedule_kind: ScheduleKind;
  at_time: string;
  weekday?: number | null;
  day_of_month?: number | null;
  calendar_dates?: string[] | null;
  timezone: string;
  window_end?: string | null;
  squareoff_at?: string | null;
  enabled: boolean;
};

export type CalendarSession = {
  session_date: string;
  session_type: string;
  opens_at: string | null;
  closes_at: string | null;
  verified: boolean;
  source_reference: string | null;
};

export type CalendarSessions = {
  schema_version: number;
  source: string;
  source_as_of: string;
  retrieved_at: string;
  exchange: string;
  segment: string;
  calendar_version: number;
  official_source_document_sha256: string;
  canonical_csv_sha256: string;
  sessions: CalendarSession[];
};

// ---------------------------------------------------------------------------
// Plan review, reservations, approvals and the projected book
// ---------------------------------------------------------------------------

export type PlanDetail = {
  plan_id: string;
  proposal_id: string;
  strategy_id: string;
  account_id: string;
  plan_kind: string;
  plan_hash: string;
  logical_plan: Record<string, unknown>;
  resolved_plan: Record<string, unknown>;
  pinned_universe_revision_id: string | null;
  pinned_member_hash: string | null;
  pinned_catalog_generation: string;
  invalidation_state: Record<string, unknown>;
};

export type AdmissionVerdict = {
  admitted: boolean;
  rejection_reason: string | null;
  detail: Record<string, unknown>;
};

export type ReservationRow = {
  reservation_id: string;
  plan_id: string;
  strategy_id: string;
  account_id: string;
  evaluation_id: string;
  execution_environment: string;
  status: string;
  reserved_notional_inr: number;
  margin_evidence: Record<string, unknown>;
  margin_as_of: string | null;
  valid_until: string | null;
  renewed_at: string | null;
  released_at: string | null;
  release_reason: string | null;
};

export type ApprovalRow = {
  approval_id: string;
  plan_id: string;
  strategy_id: string;
  account_id: string;
  reservation_id: string;
  plan_hash: string;
  exposure_snapshot_version: number;
  exposure_snapshot_hash: string | null;
  reconciliation_version: number;
  catalog_generation: string;
  session_product_snapshot: Record<string, unknown>;
  actor_id: string;
  actor_kind: string;
  authorization_evidence: Record<string, unknown>;
  status: string;
  valid_from: string | null;
  valid_until: string | null;
  structural_validity: Record<string, unknown>;
};

export type HostedPositionRow = {
  identity_kind: string;
  identity_key: string;
  product: string;
  instrument_token: number;
  exchange: string;
  tradingsymbol: string;
  net_quantity: number;
  unresolved_reason: string | null;
};

export type HostedPositionList = {
  strategy_id: string;
  environment: string;
  positions: HostedPositionRow[];
};

// ---------------------------------------------------------------------------
// B2.6a: owner-facing option-run operations
// ---------------------------------------------------------------------------

/** `"unknown"` coverage means the list is NOT complete; never read it as empty. */
export type OptionCoverage = "known" | "unknown";

export type OptionRunLeg = {
  leg_id: string;
  tradingsymbol: string;
  side: "BUY" | "SELL" | string;
  role: "hedge" | "short" | "naked" | null;
  ratio: number;
  /** Frozen target quantity. */
  quantity: number;
  /** Signed, from the run's OWN confirmed fills; null if unreadable. */
  own_open_quantity: number | null;
  state: "open" | "pending" | "failed" | "flat" | string;
};

export type OptionRunStatus =
  | "created"
  | "entry_previewed"
  | "entering"
  | "entered"
  | "partial_entry"
  | "cleanup_required"
  | "adjusting"
  | "exit_previewed"
  | "exiting"
  | "partial_exit"
  | "exited"
  | "settled"
  | "unknown";

export type OptionRun = {
  option_run_id: string;
  status: OptionRunStatus | string;
  structure_generation: number;
  structure_digest: string;
  underlying: string;
  expiry: string;
  product: string;
  protective_exit_unresolved: boolean;
  coverage: OptionCoverage | string;
  legs: OptionRunLeg[];
  /** status in partial_entry|partial_exit|cleanup_required|adjusting */
  repairable: boolean;
  /** Placeholder until B2.4; the UI shows "protection owner: not yet available". */
  protection_owner: string | null;
};

export type OptionRunList = {
  strategy_id: string;
  coverage: OptionCoverage | string;
  coverage_reason: string;
  runs: OptionRun[];
};

export type OptionRunEdge = {
  plan_id: string;
  phase: "entry" | "adjust" | "exit" | string;
  created_at: string | null;
};

export type OptionRunFrozen = {
  protection_policy: Record<string, unknown> | null;
  max_loss: Record<string, unknown> | null;
  expiry_policy: string | null;
};

export type OptionRunRefusal = {
  request_id: string;
  plan_id: string;
  refusal_code: string;
  stage: string;
  detail: Record<string, unknown>;
  at: string | null;
};

export type OptionRunGreeks = {
  available: boolean;
  reason: string | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
};

export type OptionRunPnl = {
  available: boolean;
  reason: string | null;
  premium: number | null;
  mtm: number | null;
};

export type OptionRunDetail = {
  run: OptionRun;
  /** entry|adjust|exit, oldest first. */
  edges: OptionRunEdge[];
  frozen: OptionRunFrozen;
  /** newest first, max 20. */
  refusals: OptionRunRefusal[];
  greeks: OptionRunGreeks;
  pnl: OptionRunPnl;
};

/** One bounded, risk-reducing close action the repair would submit. */
export type OptionRunRepairPlanLeg = {
  tradingsymbol: string;
  transaction_type: string;
  quantity: number;
  exchange?: string | null;
  product?: string | null;
  order_type?: string | null;
};

export type OptionRunRepairState = "flat" | "residual" | "ambiguous" | "not_repairable" | string;

/**
 * One plan step the repair assessment could not resolve on its own. Populated
 * only while the assessment is `ambiguous` with reason `adjust_in_flight`;
 * `state` is the step's newest trail word (`submitted`, `partially_filled`,
 * …), or `""` when the platform cannot read it.
 */
export type OptionRunRepairUnresolvedStep = {
  plan_id: string;
  step_no: number;
  state: string;
  order_id: string | null;
};

export type OptionRunRepairAssessment = {
  option_run_id: string;
  status: string;
  state: OptionRunRepairState;
  reason_code: string | null;
  reasons: string[];
  evidence_digest: string;
  close_plan: OptionRunRepairPlanLeg[];
  evidence: Record<string, unknown>;
  detail: Record<string, unknown>;
  /** Additive; servers that predate this field report no unresolved steps. */
  unresolved_steps?: OptionRunRepairUnresolvedStep[];
};

export type OptionRunRepairActionPayload = {
  action: "close_flat" | "close_residual";
  evidence_digest: string;
};

export type OptionRunRepairActionResult = {
  option_run_id: string;
  action: string;
  state: string;
  run_status: string;
  evidence_digest: string;
  audit_id: string | null;
  submission: Record<string, unknown>;
};

// ---------------------------------------------------------------------------
// B2.6b: owner-facing safe actions (cancel pending work, dead-submission
// disposition). All routes are under `/api/strategies/{strategy_id}`.
// ---------------------------------------------------------------------------

/** Server classification; the UI never re-derives eligibility itself. */
export type PendingWorkEligibility = "eligible" | "ineligible" | string;

export type PendingWorkItem = {
  plan_id: string;
  step_no: number;
  order_id: string | null;
  remaining_quantity: number | null;
  eligibility: PendingWorkEligibility;
  /** Human-readable via `withRefusalCopy`; null only when eligible. */
  reason_code: string | null;
};

/** `"unknown"` coverage means this candidate set is NOT proven complete. */
export type PendingWorkCoverage = "known" | "unknown";

export type PendingWorkPreview = {
  coverage: PendingWorkCoverage | string;
  evidence_digest: string;
  items: PendingWorkItem[];
};

export type CancelPendingPayload = {
  /** Pinned to the digest read from the preview; a stale digest refuses. */
  evidence_digest: string;
  reason: "owner_cancel" | string;
};

export type OwnerActionStatus = "complete" | "accepted" | "blocked" | string;

/** The shared owner-action response envelope from design §5. */
export type OwnerActionResult = {
  status: OwnerActionStatus;
  action_id: string;
  evidence_digest: string;
  items: PendingWorkItem[];
  refusal?: string | null;
  audit_id?: string | null;
};

/**
 * One unanswered plan-step submission's terminal outcome. Only these five are
 * ever recognized; the owner picks among what the server allows, never types
 * one.
 */
export type DeadSubmissionDisposition =
  | "filled"
  | "rejected"
  | "cancelled"
  | "failed_never_submitted"
  | "failed_residual_abandoned"
  | string;

export type DeadSubmissionEvidence = {
  trail_state: string;
  source: string;
  status: string;
  filled_quantity: number | null;
  remaining_quantity: number | null;
  /** ONLY these are offered as choices; never invented client-side. */
  allowed_dispositions: DeadSubmissionDisposition[];
  evidence_digest: string;
};

export type DeadSubmissionActionPayload = {
  evidence_digest: string;
  disposition: DeadSubmissionDisposition;
  reason: string;
};

export type DeadSubmissionActionResult = {
  status: OwnerActionStatus;
  action_id: string;
  evidence_digest: string;
  refusal?: string | null;
  audit_id?: string | null;
  detail?: Record<string, unknown>;
};
