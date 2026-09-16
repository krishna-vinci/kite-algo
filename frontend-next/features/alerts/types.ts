/**
 * Response shapes for the Phase 6 operator API (`/api/alerts/*`).
 *
 * Every field here mirrors a backend response verified against
 * `backend/api/routers/alerts_operator.py` and
 * `alerts_operator_platform.py`. Where the API returns `null` to mean
 * "unknown", the type says `| null` deliberately: unknown is not zero, and the
 * UI must render the difference (see the handoff, §8).
 */

// ---------------------------------------------------------------------------
// scopes and discovery
// ---------------------------------------------------------------------------

export type AlertsScopeOption = {
  scope: string;
  is_default: boolean;
  has_data: boolean;
};

export type AlertsScopesResponse = {
  ok: boolean;
  scopes: AlertsScopeOption[];
  note: string;
};

export type CapabilityBounds = { min: number; max: number };

export type FeatureCapability = {
  params: Record<string, CapabilityBounds>;
  defaults: Record<string, number>;
  inputs: string[];
  outputs: string[];
};

export type PairCapability = {
  formula: string;
  fields: string[];
  requires: string[];
  optional: string[];
  unknown_reasons: string[];
};

export type ClockCapability = {
  latency: string;
  source: string;
  note?: string;
};

export type LimitsCapability = {
  max_stages: number;
  max_alerts: number;
  max_instruments: number;
  max_feature_stages: number;
  max_conditions_per_group: number;
  max_arithmetic_depth: number;
  max_input_chain_depth: number;
  max_consecutive_bars: number;
  max_sequence_within_bars: number;
  max_breadth_instruments: number;
  max_breadth_window_s: number;
  max_per_session: number;
};

export type ScreenerCapability = {
  attachment_triggers: string[];
  attachment_hysteresis: Record<string, string>;
  schedule_calendars: string[];
  schedule_note: string;
  stored_data_fields: string[];
  run_statuses: string[];
  tie_break: string;
};

export type AlertsCapabilities = {
  operators: Record<string, string | null>;
  fields: string[];
  fundamentals_fields: string[];
  fundamentals_source: {
    table: string;
    description: string;
    freshness_keys: string[];
    columns: Record<string, string>;
  };
  clocks: Record<string, ClockCapability>;
  clock_aliases: Record<string, string>;
  triggers: string[];
  timeframes: string[];
  sessions: string[];
  /** session name -> the exchanges that session accepts (compiler's own rule). */
  session_exchanges: Record<string, string[]>;
  features: Record<string, FeatureCapability>;
  arithmetic: string[];
  limits: LimitsCapability;
  stage_types: string[];
  pairs: Record<string, PairCapability>;
  pair_lookback_bounds: number[];
  pair_max_skew_bars: number;
  breadth_modes: Record<string, { implemented: boolean }>;
  breadth_semantics: string;
  hysteresis: { operators: string[]; threshold: string };
  session_cap_resets: string[];
  session_cap_note: string;
  universe_ref_kinds: string[];
  screener: ScreenerCapability;
  universe_source_kinds: string[];
  /** Index lists this install can actually resolve (derived server-side). */
  universe_index_source_lists: string[];
};

export type AlertsCapabilitiesResponse = {
  ok: boolean;
  capabilities: AlertsCapabilities;
};

// ---------------------------------------------------------------------------
// workflows
// ---------------------------------------------------------------------------

export type AlertsRevisionSummary = {
  revision_id: string | null;
  revision: number;
  status: string;
  canonical_hash: string | null;
  created_at: string | null;
  activated_at: string | null;
};

/**
 * Evaluation freshness for a workflow.
 *
 * `stale` is `null` when nothing has ever been evaluated — which is NOT the
 * same as `false` ("fresh"). A workflow with no subscriptions has no data to
 * be fresh about.
 */
export type AlertsFreshness = {
  last_evaluated_at: string | null;
  evaluation_age_s: number | null;
  subscription_count: number;
  stale_subscriptions: number;
  stale: boolean | null;
  stale_after_seconds: number;
};

export type AlertsWarning = {
  where: string;
  code: string;
  message: string;
  severity: string;
};

export type AlertsAlertRef = {
  id: string;
  source: string;
  trigger: string;
};

export type AlertsWorkflowKind = "alert" | "screener";

export type AlertsWorkflowSummary = {
  workflow_id: string;
  name: string;
  archived: boolean;
  archived_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  latest_revision: AlertsRevisionSummary | null;
  active_revision: AlertsRevisionSummary | null;
  kind: AlertsWorkflowKind;
  instruments: string[];
  instrument_summary: string | null;
  has_universe: boolean;
  alerts: AlertsAlertRef[];
  channels: string[];
  warnings: AlertsWarning[];
  subscription_count: number;
  freshness: AlertsFreshness;
  session?: string;
  /**
   * The first stage's simple comparison, when it is one: the list row shows
   * "crosses above 125,000" without fetching every document. Null when the rule
   * is a group, a sequence or an operand the description cannot be honest about.
   */
  rule?: {
    field: string;
    operator: string;
    value: number;
    clock: string;
    timeframe: string;
  } | null;
};

export type AlertsWorkflowListResponse = {
  ok: boolean;
  scope: string;
  workflows: AlertsWorkflowSummary[];
};

export type AlertsWorkflowDetail = AlertsWorkflowSummary & {
  document: Record<string, unknown> | null;
  revision_in_force: AlertsRevisionSummary | null;
  /**
   * Effective lifecycle from the server (archived | draft | paused | active).
   * Pause/Resume act on subscriptions while the revision stays active, so this
   * is the only field that answers "is it running?".
   */
  lifecycle_state?: "archived" | "draft" | "paused" | "active" | null;
  yaml?: string | null;
  yaml_error?: string;
};

export type AlertsWorkflowDetailResponse = {
  ok: boolean;
  scope: string;
} & AlertsWorkflowDetail;

export type AlertsWorkflowRevisionsResponse = {
  ok: boolean;
  workflow_id: string;
  limit: number;
  revisions: AlertsRevisionSummary[];
};

export type AlertsWorkflowMutationResponse = {
  ok: boolean;
  workflow_id: string;
  changed?: boolean;
  created?: boolean;
  revision?: number;
  revision_id?: string;
  revision_status?: string;
  canonical_hash?: string;
};

export type AlertsLifecycleResponse = {
  ok: boolean;
  workflow_id: string;
  state?: string;
  updated?: number;
  revision?: number;
  revision_id?: string;
  revision_status?: string;
  canonical_hash?: string;
  subscriptions_created?: number;
  note?: string;
  archived?: boolean;
  archived_at?: string | null;
  revisions_archived?: number;
};

// ---------------------------------------------------------------------------
// validation / preview
// ---------------------------------------------------------------------------

export type AlertsIssue = {
  where: string;
  code: string;
  message: string;
  severity: string;
};

export type AlertsValidateResponse = {
  ok: boolean;
  issues: AlertsIssue[];
};

export type AlertsPreviewResponse = {
  ok: boolean;
  issues: AlertsIssue[];
  instruments?: string[];
  stages?: string[];
  alerts?: string[];
  evaluation?: string;
  warmup_bars?: number;
  evaluated_observations?: number;
  would_fire?: string[];
  unknown_reasons?: string[];
  note?: string;
};

// ---------------------------------------------------------------------------
// health, events, deliveries
// ---------------------------------------------------------------------------

/**
 * Per-subscription freshness derived from durable checkpoint state.
 *
 * `stale_reason` is a vocabulary, not a boolean: `no_accepted_tick`,
 * `tick_age_exceeded`, `continuity_invalidated`, `not_an_ltp_subscription`.
 * Each implies a different operator action, so the UI must show the reason
 * rather than collapsing it into "stale".
 */
export type AlertsSubscriptionHealth = {
  subscription_id: string;
  alert_id: string;
  instrument_key: string;
  state: string;
  last_evaluated_at: string | null;
  evaluation_age_s: number | null;
  last_tick_received_at: string | null;
  last_tick_ts: string | null;
  tick_age_s: number | null;
  stale: boolean | null;
  stale_reason: string | null;
  continuity_invalidated_at: string | null;
  continuity_invalidation_reason: string | null;
  received_at_is_receipt_not_event_time: boolean;
  quarantined_until: string | null;
  failures: number;
  last_error: string | null;
  last_failure_at: string | null;
};

export type AlertsTaskHealth = {
  alive: boolean;
  restarts: number;
  backoff_s: number | null;
  last_started_at?: string | null;
  last_exit_reason?: string | null;
};

export type AlertsRuntimeHealth = {
  available: boolean;
  reason?: string;
  quarantined?: Record<string, string>;
  subscription_failures?: Record<string, { failures: number; last_error?: string; last_failure_at?: string }>;
  tasks?: Record<string, AlertsTaskHealth>;
  /** Rejection counters keyed by reason (`stale_tick`, `future_tick`, ...). */
  rejected_ticks?: Record<string, number>;
  stale_tick_instruments?: number;
  never_ticked_instruments?: number;
  ltp_freshness_enabled?: boolean;
  startup_error?: string | null;
  last_health_at?: string | null;
  [key: string]: unknown;
};

export type AlertsWorkflowHealthResponse = {
  ok: boolean;
  workflow_id: string;
  lifecycle: {
    active: boolean;
    archived: boolean;
    archived_at: string | null;
    active_revision: AlertsRevisionSummary | null;
  };
  subscriptions: AlertsSubscriptionHealth[];
  runtime: AlertsRuntimeHealth;
  /**
   * DURABLE per-reason suppression counts (e.g. `session_cap`), persisted when
   * the notification was skipped. Unlike `runtime`, these need no worker health
   * file — they come from the database.
   */
  suppressions?: Record<string, number>;
  note: string;
};

/** Non-secret producer credential metadata, for later revocation by token id. */
export type AlertsProducerCredentialMetadata = {
  token_id: string;
  status: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type AlertsProducerCredentialsResponse = {
  ok: boolean;
  producer: string;
  credentials: AlertsProducerCredentialMetadata[];
  note: string;
};

export type AlertsPlatformHealthResponse = {
  ok: boolean;
  runtime: AlertsRuntimeHealth;
};

export type AlertsSignalEvent = {
  event_id: string;
  subscription_id?: string;
  occurrence_key?: string;
  fired_at: string | null;
  evidence: Record<string, unknown>;
};

export type AlertsEventsResponse = {
  ok: boolean;
  workflow_id: string;
  limit: number;
  offset: number;
  total: number;
  events: AlertsSignalEvent[];
};

export type AlertsDeliveryAttempt = {
  attempt_no: number;
  outcome: string;
  detail: string | null;
  provider_id: string | null;
  created_at: string | null;
};

export type AlertsDelivery = {
  delivery_id: string;
  event_id: string;
  channel_id: string | null;
  channel_name: string | null;
  provider: string | null;
  status: string;
  attempts: number;
  next_attempt_at: string | null;
  delivered_at: string | null;
  last_error: string | null;
  created_at: string | null;
  fired_at: string | null;
  attempt_log: AlertsDeliveryAttempt[];
};

export type AlertsDeliveriesResponse = {
  ok: boolean;
  workflow_id: string;
  limit: number;
  offset: number;
  deliveries: AlertsDelivery[];
  note: string;
};

// ---------------------------------------------------------------------------
// channels
// ---------------------------------------------------------------------------

export type AlertsChannel = {
  channel_id: string;
  name: string;
  provider: string;
  destination: Record<string, unknown>;
  secret_env: string | null;
  enabled: boolean;
  created_at: string | null;
};

export type AlertsChannelsResponse = {
  ok: boolean;
  channels: AlertsChannel[];
  note: string;
};

export type AlertsChannelTestResponse = {
  ok: boolean;
  status: string;
  provider_id: string | null;
  detail: string;
};

// ---------------------------------------------------------------------------
// tokens
// ---------------------------------------------------------------------------

export type AlertsTokenPreset = {
  id: string;
  actions: string[];
  description: string;
};

export type AlertsTokenPresetsResponse = {
  ok: boolean;
  presets: AlertsTokenPreset[];
  all_actions: string[];
  modes: string[];
  account_scope: string;
  note: string;
};

export type AlertsToken = {
  token_id: string;
  label: string | null;
  account_scope: string | null;
  allowed_actions: string[];
  allowed_modes: string[];
  status: string | null;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  scope_matches_operator: boolean;
};

export type AlertsTokensResponse = {
  ok: boolean;
  tokens: AlertsToken[];
  authorized_scopes: string[];
};

export type AlertsTokenCreateResponse = {
  ok: boolean;
  token: string;
  token_id: string;
  account_scope: string;
  allowed_actions: string[];
  allowed_modes: string[];
  reveal_once: true;
  note: string;
};

// ---------------------------------------------------------------------------
// universes
// ---------------------------------------------------------------------------

export type AlertsUniverseRevisionSummary = {
  revision: number;
  member_count: number;
  source_generation: string | null;
  resolved_at: string | null;
  created_at: string | null;
};

export type AlertsUniverseSummary = {
  universe_id: string;
  name: string;
  kind: string;
  source_config: Record<string, unknown>;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  latest_revision: AlertsUniverseRevisionSummary | null;
};

export type AlertsUniversesResponse = {
  ok: boolean;
  universes: AlertsUniverseSummary[];
};

export type AlertsUniverseDetail = AlertsUniverseSummary & {
  latest_members: string[] | null;
  latest_coverage: Record<string, unknown> | null;
};

export type AlertsUniverseDetailResponse = {
  ok: boolean;
} & AlertsUniverseDetail;

export type AlertsUniverseRevisionItem = {
  revision_id: string;
  revision: number;
  members: string[];
  member_count: number;
  source_generation: string | null;
  coverage: Record<string, unknown>;
  resolved_at: string | null;
  created_at: string | null;
};

export type AlertsUniverseRevisionsResponse = {
  ok: boolean;
  universe_id: string;
  name: string;
  limit: number;
  revisions: AlertsUniverseRevisionItem[];
};

export type AlertsUniversePreviewResponse = {
  ok: boolean;
  kind: string;
  members: string[];
  rejected: unknown[];
  source_generation: string | null;
  coverage: Record<string, unknown>;
};

export type AlertsUniverseResolveResponse = AlertsUniversePreviewResponse & {
  universe_id: string;
  name: string;
  revision: number;
};

// ---------------------------------------------------------------------------
// screeners
// ---------------------------------------------------------------------------

/**
 * One screener run. Mirrors the worker's `RunOut`
 * (`backend/api/schemas/screeners.py`): `created_at`/`completed_at` — there is
 * no `started_at`/`finished_at`, and the member count comes from the run-detail
 * response, not the run row.
 */
export type AlertsScreenerRun = {
  run_id: string;
  workflow_id?: string;
  workflow_revision_id?: string;
  occurrence_key?: string;
  scheduled_for?: string | null;
  triggered_by?: string;
  status: string;
  universe_revision?: number | null;
  as_of?: string | null;
  coverage?: Record<string, unknown>;
  data_freshness?: Record<string, unknown> | null;
  failure_reason?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  [key: string]: unknown;
};

export type AlertsScreenerRunsResponse = {
  ok: boolean;
  workflow_id: string;
  runs: AlertsScreenerRun[];
  limit: number;
  offset: number;
  note: string;
};

export type AlertsScreenerRunMember = {
  instrument_key: string;
  passed: boolean;
  exclusion_reason: string | null;
  rank: number | null;
  score: number | null;
  values: Record<string, unknown>;
};

export type AlertsScreenerRunResponse = {
  ok: boolean;
  run: AlertsScreenerRun;
  members: AlertsScreenerRunMember[];
  member_count: number;
  limit: number;
  offset: number;
};

export type AlertsScreenerAttachmentMember = {
  instrument_key: string;
  present: boolean;
  last_rank: number | null;
  consecutive_absent: number;
  last_complete_run_id: string | null;
  updated_at: string | null;
};

export type AlertsScreenerAttachment = {
  attachment_id: string;
  trigger: string | null;
  channels: string[];
  hysteresis: {
    entry_rank: number | null;
    exit_rank: number | null;
    exit_after: number | null;
    top_n: number | null;
    rank_delta: number | null;
    initial_match: boolean;
  };
  membership_count: number;
  members: AlertsScreenerAttachmentMember[];
};

export type AlertsScreenerAttachmentsResponse = {
  ok: boolean;
  workflow_id: string;
  revision: number;
  revision_id: string;
  attachments: AlertsScreenerAttachment[];
  note: string;
};

// ---------------------------------------------------------------------------
// external producers
// ---------------------------------------------------------------------------

export type AlertsProducer = {
  name: string;
  value_schema: Record<string, unknown> | null;
  default_ttl_s: number | null;
  status?: string;
  created_at?: string | null;
  revoked_at?: string | null;
  [key: string]: unknown;
};

export type AlertsProducersResponse = {
  ok: boolean;
  producers: AlertsProducer[];
  note: string;
};

export type AlertsProducerCredentialResponse = {
  ok: boolean;
  token_id: string;
  secret: string;
  reveal_once: true;
  note: string;
};

export type AlertsSignalValue = {
  value_id: string;
  instrument_key: string | null;
  event_time: string | null;
  received_at: string | null;
  expires_at: string | null;
  status: string;
  value: Record<string, unknown> | null;
  idempotency_key: string | null;
};

export type AlertsSignalValuesResponse = {
  ok: boolean;
  producer: string;
  limit: number;
  offset: number;
  total: number;
  values: AlertsSignalValue[];
};

export type AlertsSignalsHealthResponse = {
  ok: boolean;
  limits: {
    max_payload_bytes: number;
    max_fields: number;
    max_string_length: number;
    max_rows_per_producer: number;
    retention_s: number;
    max_future_skew_s: number;
    max_lateness_s: number;
  };
  note: string;
  purge_available: boolean;
  producers: Array<Record<string, unknown>>;
};

// ---------------------------------------------------------------------------
// instruments (catalog search)
// ---------------------------------------------------------------------------

export type AlertsInstrumentResult = {
  public_key: string;
  symbol: string;
  exchange: string;
  segment: string;
  name: string | null;
  instrument_type: string | null;
  expiry: string | null;
  strike: number | null;
  option_type: string | null;
  underlying: string | null;
  lot_size: number | null;
  lifecycle_status: string | null;
};

export type AlertsInstrumentSearchResponse = {
  ok: boolean;
  query: string;
  results: AlertsInstrumentResult[];
  note: string;
};

// ---------------------------------------------------------------------------
// canvas layout
// ---------------------------------------------------------------------------

export type AlertsCanvasNode = {
  node_id: string;
  x: number;
  y: number;
  collapsed: boolean;
  updated_at?: string | null;
};

export type AlertsCanvasLayoutResponse = {
  ok: boolean;
  workflow_id: string;
  nodes: AlertsCanvasNode[];
  contract: {
    namespaces: string[];
    node_id_format: string;
    max_abs_coordinate: number;
    max_node_id_length: number;
    note: string;
  };
};

/** Per-member candle availability for a screener (GET .../data-status). */
export type AlertsScreenerMemberCandles = {
  instrument_key: string;
  bars: number;
  required_bars: number;
  sufficient: boolean;
  last_candle_ts: string | null;
  warming: boolean;
};

export type AlertsScreenerDataStatusResponse = {
  ok: boolean;
  workflow_id: string;
  warming_supported: boolean;
  resolution_ok?: boolean;
  member_count?: number;
  required_bars?: number;
  members_needing_candles?: number;
  status?: "complete" | "warming" | "unavailable";
  members?: AlertsScreenerMemberCandles[];
  note?: string;
};

/** Result of a bounded candle-warming call (POST .../warm-candles). */
export type AlertsScreenerWarmResponse = {
  ok: boolean;
  workflow_id: string;
  status: string;
  requested: number;
  warmed: number;
  fresh: number;
  unavailable: number;
  skipped: number;
  expired: number;
  required_bars: number;
  catalog_generation?: string | null;
  budget_exhausted: boolean;
  duration_s: number;
  members?: Array<{
    instrument_key: string;
    status: string;
    broker_token?: number | null;
    bars?: number;
    required_bars?: number;
    detail?: string | null;
  }>;
};
