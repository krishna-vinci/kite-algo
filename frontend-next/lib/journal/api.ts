import { apiFetch } from "@/lib/api/client";
import type {
  AnalysisPeriod,
  BenchmarkComparison,
  CalendarDay,
  JournalFilterParams,
  JournalEnvironment,
  JournalEpisode,
  JournalInsight,
  JournalNote,
  JournalNoteRevision,
  JournalRule,
  JournalRun,
  JournalSummary,
  JournalTimelineEvent,
  JournalTrade,
  JournalV2AnalyticsSummary,
  JournalV2PaperLiveComparison,
  JournalV2StrategyAnalytics,
  JournalV2UnresolvedQueue,
  Paginated,
  ReviewQueueItem,
  ReviewUpdatePayload,
  RuleCreatePayload,
  RuleUpdatePayload,
  StrategyPerformance,
} from "./types";

function toSearchParams(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter((entry): entry is [string, string] => entry[1] !== undefined);
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : "";
}

function toNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function mapPeriod(period: AnalysisPeriod): string {
  return period === "inception" ? "since_inception" : period;
}

function requireEnvironmentId(environmentId: string | undefined | null, helperName: string): string {
  const normalized = String(environmentId ?? "").trim();
  if (!normalized) {
    throw new Error(`${helperName} requires environment_id`);
  }
  return normalized;
}

function mapReviewState(value: unknown): JournalRun["review_status"] {
  const normalized = String(value ?? "pending").toLowerCase();
  if (normalized === "reviewed") return "completed";
  if (normalized === "waived") return "skipped";
  if (normalized === "in_progress") return "in_progress";
  return "pending";
}

function mapRuleState(value: unknown): JournalRule["state"] {
  const normalized = String(value ?? "active").toLowerCase();
  if (normalized === "draft" || normalized === "reinforced" || normalized === "decaying" || normalized === "retired") {
    return normalized as JournalRule["state"];
  }
  return "active";
}

function mapRuleTypeToCategory(value: unknown): string {
  const normalized = String(value ?? "strategy_specific").toLowerCase();
  if (normalized === "risk_execution") return "risk";
  if (normalized === "psychological") return "process";
  if (normalized === "universal") return "general";
  return "entry";
}

function mapCategoryToRuleType(category: string): string {
  if (category === "risk" || category === "position-sizing") return "risk_execution";
  if (category === "process") return "psychological";
  if (category === "general") return "universal";
  return "strategy_specific";
}

function normalizeRunSummary(response: Record<string, unknown>): JournalSummary {
  return {
    period: (response.period === "since_inception" ? "inception" : ((response.period as AnalysisPeriod) ?? "month")),
    run_count: Number(response.run_count ?? 0),
    closed_run_count: Number(response.closed_run_count ?? 0),
    gross_profit: toNumber(response.gross_profit) ?? 0,
    gross_loss: toNumber(response.gross_loss) ?? 0,
    net_pnl: toNumber(response.net_pnl) ?? 0,
    total_charges: toNumber(response.total_fees) ?? 0,
    win_rate: toNumber(response.win_rate),
    average_win: toNumber(response.average_win),
    average_loss: toNumber(response.average_loss),
    profit_factor: toNumber(response.profit_factor),
    expectancy: toNumber(response.expectancy),
    cumulative_return: toNumber(response.cumulative_return),
    benchmark_return: toNumber(response.benchmark_return),
    excess_return: toNumber(response.excess_return),
    max_drawdown: toNumber(response.max_drawdown),
    drawdown_duration_days: toNumber(response.max_drawdown_duration),
    sharpe_ratio: toNumber(response.sharpe_ratio),
    sortino_ratio: toNumber(response.sortino_ratio),
    best_streak: toNumber(response.max_win_streak),
    worst_streak: toNumber(response.max_loss_streak),
    review_completion_rate: toNumber(response.review_completion_rate),
    rule_adherence_rate: toNumber(response.rule_adherence_rate),
  };
}

function normalizeRun(item: Record<string, unknown>): JournalRun {
  return {
    id: String(item.id ?? ""),
    strategy_family: String(item.strategy_family ?? "options_strategy") as JournalRun["strategy_family"],
    strategy_name: String(item.strategy_name ?? "Unspecified"),
    status: String(item.status ?? "open") as JournalRun["status"],
    review_status: mapReviewState(item.review_state),
    entry_surface: item.entry_surface ? String(item.entry_surface) : null,
    opened_at: String(item.started_at ?? new Date().toISOString()),
    closed_at: item.ended_at ? String(item.ended_at) : null,
    net_pnl: toNumber(item.net_pnl),
    total_charges: toNumber(item.total_fees),
    notes: typeof item.metadata === "object" && item.metadata && "review_notes" in (item.metadata as Record<string, unknown>)
      ? String((item.metadata as Record<string, unknown>).review_notes ?? "")
      : null,
    tags: [],
    decision_events: [],
  };
}

function normalizeJournalEnvironment(item: Record<string, unknown>): JournalEnvironment {
  return {
    id: String(item.id ?? ""),
    mode: String(item.mode ?? "paper") as JournalEnvironment["mode"],
    account_scope: String(item.account_scope ?? ""),
    display_name: item.display_name != null ? String(item.display_name) : null,
    broker_user_id: item.broker_user_id != null ? String(item.broker_user_id) : null,
    paper_account_key: item.paper_account_key != null ? String(item.paper_account_key) : null,
    environment_epoch: Number(item.environment_epoch ?? 1),
    metadata: (item.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

function normalizeJournalEpisode(item: Record<string, unknown>): JournalEpisode {
  return {
    id: String(item.id ?? ""),
    environment_id: String(item.environment_id ?? ""),
    execution_context_id: String(item.execution_context_id ?? ""),
    episode_seq: Number(item.episode_seq ?? 0),
    status: String(item.status ?? "draft"),
    opened_at: String(item.opened_at ?? new Date().toISOString()),
    closed_at: item.closed_at != null ? String(item.closed_at) : null,
    metadata: (item.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

function normalizeJournalTimelineEvent(item: Record<string, unknown>): JournalTimelineEvent {
  return {
    id: String(item.id ?? ""),
    environment_id: String(item.environment_id ?? ""),
    episode_id: item.episode_id != null ? String(item.episode_id) : null,
    execution_context_id: item.execution_context_id != null ? String(item.execution_context_id) : null,
    subject_type: String(item.subject_type ?? ""),
    subject_id: String(item.subject_id ?? ""),
    event_type: String(item.event_type ?? ""),
    channel: item.channel != null ? String(item.channel) : null,
    actor_type: String(item.actor_type ?? "system"),
    correlation_id: item.correlation_id != null ? String(item.correlation_id) : null,
    causation_id: item.causation_id != null ? String(item.causation_id) : null,
    occurred_at: String(item.occurred_at ?? new Date().toISOString()),
    payload: (item.payload as Record<string, unknown> | undefined) ?? {},
  };
}

function normalizeJournalNote(item: Record<string, unknown>): JournalNote {
  return {
    id: String(item.id ?? ""),
    environment_id: String(item.environment_id ?? ""),
    subject_type: String(item.subject_type ?? ""),
    subject_id: String(item.subject_id ?? ""),
    episode_id: item.episode_id != null ? String(item.episode_id) : null,
    note_type: String(item.note_type ?? ""),
    title: String(item.title ?? ""),
    body_markdown: String(item.body_markdown ?? ""),
    body_text: item.body_text != null ? String(item.body_text) : "",
    body_json: (item.body_json as Record<string, unknown> | null | undefined) ?? null,
    tags: Array.isArray(item.tags) ? item.tags.map((tag) => String(tag)) : [],
    metadata: (item.metadata as Record<string, unknown> | undefined) ?? {},
    updated_at: String(item.updated_at ?? new Date().toISOString()),
  };
}

type JournalNotesQuery = {
  environment_id: string;
  subject_type?: string;
  subject_id?: string;
  episode_id?: string;
  note_type?: string;
  limit?: number;
  offset?: number;
};

type CreateJournalNotePayload = {
  environment_id: string;
  subject_type: string;
  subject_id: string;
  episode_id?: string;
  note_type: string;
  title: string;
  body_markdown: string;
  body_json?: Record<string, unknown>;
  effective_at?: string;
  author_id?: string;
  tags?: string[];
  metadata?: Record<string, unknown>;
};

type UpdateJournalNotePayload = {
  environment_id: string;
  subject_type: string;
  subject_id: string;
  title?: string;
  body_markdown?: string;
  body_json?: Record<string, unknown>;
  tags?: string[];
  metadata?: Record<string, unknown>;
  editor_id?: string;
  change_reason?: string;
};

export async function fetchJournalSummary(params: { period?: AnalysisPeriod } & JournalFilterParams = {}): Promise<JournalSummary> {
  const response = await apiFetch<Record<string, unknown>>(
    `/api/journal/summary${toSearchParams({
      period: params.period ? mapPeriod(params.period) : undefined,
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  return normalizeRunSummary(response);
}

export async function fetchBenchmarkComparison(params: { period?: AnalysisPeriod } & JournalFilterParams = {}): Promise<BenchmarkComparison> {
  const response = await apiFetch<Record<string, unknown>>(
    `/api/journal/benchmark${toSearchParams({
      period: params.period ? mapPeriod(params.period) : undefined,
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  return {
    benchmark_id: String(response.benchmark_id ?? "NIFTY50"),
    benchmark_name: String(response.benchmark_name ?? response.benchmark_id ?? "NIFTY50"),
    period: (params.period ?? "month"),
    portfolio_return: toNumber(response.portfolio_return),
    benchmark_return: toNumber(response.benchmark_return),
    excess_return: toNumber(response.excess_return),
    portfolio_series: Array.isArray(response.portfolio_series)
      ? (response.portfolio_series as Array<Record<string, unknown>>).map((item) => ({
          date: String(item.date ?? ""),
          value: toNumber(item.value) ?? 0,
        }))
      : [],
    benchmark_series: Array.isArray(response.benchmark_series)
      ? (response.benchmark_series as Array<Record<string, unknown>>).map((item) => ({
          date: String(item.date ?? ""),
          value: toNumber(item.value) ?? 0,
        }))
      : [],
  };
}

export async function fetchCalendar(params: { month?: string; year?: string } & JournalFilterParams = {}): Promise<CalendarDay[]> {
  const month = params.month ? Number(params.month) : undefined;
  const year = params.year ? Number(params.year) : undefined;
  const startDay = year && month ? new Date(Date.UTC(year, month - 1, 1)) : null;
  const endDay = year && month ? new Date(Date.UTC(year, month, 0)) : null;
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/calendar${toSearchParams({
      start_day: startDay ? startDay.toISOString().slice(0, 10) : undefined,
      end_day: endDay ? endDay.toISOString().slice(0, 10) : undefined,
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  return (response.items ?? []).map((item) => ({
    date: String(item.trading_day ?? ""),
    net_pnl: toNumber(item.net_pnl) ?? 0,
    run_count: Number(item.run_count ?? 0),
    win_count: Number(item.winning_trade_count ?? 0),
    loss_count: Number(item.losing_trade_count ?? 0),
  }));
}

export async function fetchJournalRuns(params: { page?: number; page_size?: number; status?: string } & JournalFilterParams = {}): Promise<Paginated<JournalRun>> {
  const pageSize = params.page_size ?? 20;
  const response = await apiFetch<{ items?: Array<Record<string, unknown>>; total?: number; page?: number; page_size?: number }>(
    `/api/journal/runs${toSearchParams({
      page: String(params.page ?? 1),
      page_size: String(pageSize),
      status: params.status,
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  const items = (response.items ?? []).map(normalizeRun);
  return {
    items,
    total: Number(response.total ?? items.length),
    page: Number(response.page ?? params.page ?? 1),
    page_size: Number(response.page_size ?? pageSize),
  };
}

export async function fetchJournalRun(runId: string): Promise<JournalRun> {
  const response = await apiFetch<Record<string, unknown>>(`/api/journal/runs/${runId}`);
  const run = normalizeRun((response.run as Record<string, unknown>) ?? {});
  return {
    ...run,
    notes:
      typeof (response.run as Record<string, unknown> | undefined)?.metadata === "object" && (response.run as Record<string, unknown>)?.metadata
        ? String((((response.run as Record<string, unknown>).metadata as Record<string, unknown>).review_notes as string | undefined) ?? "") || null
        : null,
    decision_events: Array.isArray(response.decision_events)
      ? (response.decision_events as Array<Record<string, unknown>>).map((event) => ({
          id: String(event.id ?? ""),
          event_type: String(event.decision_type ?? "note"),
          description: String(event.summary ?? ""),
          created_at: String(event.occurred_at ?? new Date().toISOString()),
        }))
      : [],
  };
}

export async function fetchReviewQueue(): Promise<ReviewQueueItem[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>("/api/journal/review-queue");
  return (response.items ?? []).map((item) => ({
    run_id: String(item.id ?? ""),
    strategy_name: String(item.strategy_name ?? "Unspecified"),
    strategy_family: String(item.strategy_family ?? "options_strategy") as ReviewQueueItem["strategy_family"],
    status: String(item.status ?? "open") as ReviewQueueItem["status"],
    review_status: mapReviewState(item.review_state),
    net_pnl: toNumber(item.net_pnl),
    closed_at: item.ended_at ? String(item.ended_at) : null,
    opened_at: String(item.started_at ?? new Date().toISOString()),
  }));
}

export async function updateRunReview(runId: string, payload: ReviewUpdatePayload): Promise<void> {
  await apiFetch(`/api/journal/runs/${runId}/review`, {
    method: "PATCH",
    json: payload,
  });
}

export async function fetchTrades(params: { page?: number; page_size?: number; run_id?: string } & JournalFilterParams = {}): Promise<Paginated<JournalTrade>> {
  const pageSize = params.page_size ?? 50;
  const response = await apiFetch<{ items?: Array<Record<string, unknown>>; total?: number; page?: number; page_size?: number }>(
    `/api/journal/trades${toSearchParams({
      page: String(params.page ?? 1),
      page_size: String(pageSize),
      run_id: params.run_id,
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  const items = (response.items ?? []).map((item) => ({
    id: String(item.id ?? item.trade_id ?? item.source_fact_key ?? ""),
    run_id: String(item.run_id ?? ""),
    tradingsymbol: String(item.tradingsymbol ?? item.strategy_name ?? "Unknown"),
    transaction_type: String(item.side ?? "BUY").toUpperCase() as JournalTrade["transaction_type"],
    quantity: Number(item.quantity ?? 0),
    price: toNumber(item.price) ?? 0,
    executed_at: String(item.fill_timestamp ?? new Date().toISOString()),
    charges: toNumber(item.fees_total),
  }));
  return {
    items,
    total: Number(response.total ?? items.length),
    page: Number(response.page ?? params.page ?? 1),
    page_size: Number(response.page_size ?? pageSize),
  };
}

export async function fetchStrategies(params: JournalFilterParams = {}): Promise<StrategyPerformance[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/strategies${toSearchParams({
      strategy_family: params.strategy_family,
      execution_mode: params.execution_mode,
    })}`,
  );
  return (response.items ?? []).map((item) => ({
    strategy_family: String(item.strategy_family ?? "options_strategy") as StrategyPerformance["strategy_family"],
    strategy_name: String(item.strategy_name ?? "Unspecified"),
    run_count: Number(item.run_count ?? 0),
    closed_count: Number(item.closed_run_count ?? 0),
    net_pnl: toNumber(item.net_pnl) ?? 0,
    win_rate: toNumber(item.win_rate),
    avg_pnl: toNumber(item.avg_pnl),
    profit_factor: toNumber(item.profit_factor),
    best_run_pnl: toNumber(item.best_run_pnl),
    worst_run_pnl: toNumber(item.worst_run_pnl),
  }));
}

export async function fetchRules(): Promise<JournalRule[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>("/api/journal/rules");
  return (response.items ?? []).map((item) => ({
    id: String(item.id ?? ""),
    title: String(item.title ?? "Untitled rule"),
    description: String(item.description ?? ""),
    state: mapRuleState(item.status),
    category: mapRuleTypeToCategory(item.rule_type),
    adherence_rate: toNumber(item.adherence_rate),
    total_checks: Number(item.total_checks ?? 0),
    created_at: String(item.created_at ?? new Date().toISOString()),
    updated_at: String(item.updated_at ?? item.created_at ?? new Date().toISOString()),
  }));
}

export async function createRule(payload: RuleCreatePayload): Promise<JournalRule> {
  const created = await apiFetch<Record<string, unknown>>("/api/journal/rules", {
    method: "POST",
    json: {
      title: payload.title,
      description: payload.description,
      rule_type: mapCategoryToRuleType(payload.category),
      enforcement_level: "soft_warning",
      status: "active",
      metadata: { category: payload.category },
    },
  });
  return {
    id: String(created.id ?? ""),
    title: String(created.title ?? payload.title),
    description: String(created.description ?? payload.description),
    state: mapRuleState(created.status),
    category: payload.category,
    adherence_rate: toNumber(created.adherence_rate),
    total_checks: Number(created.total_checks ?? 0),
    created_at: String(created.created_at ?? new Date().toISOString()),
    updated_at: String(created.updated_at ?? created.created_at ?? new Date().toISOString()),
  };
}

export async function updateRule(ruleId: string, payload: RuleUpdatePayload): Promise<JournalRule> {
  const updated = await apiFetch<Record<string, unknown>>(`/api/journal/rules/${ruleId}`, {
    method: "PATCH",
    json: {
      title: payload.title,
      description: payload.description,
      rule_type: payload.category ? mapCategoryToRuleType(payload.category) : undefined,
      status: payload.state,
      metadata: payload.category ? { category: payload.category } : undefined,
    },
  });
  return {
    id: String(updated.id ?? ruleId),
    title: String(updated.title ?? payload.title ?? "Untitled rule"),
    description: String(updated.description ?? payload.description ?? ""),
    state: mapRuleState(updated.status ?? payload.state),
    category: payload.category ?? mapRuleTypeToCategory(updated.rule_type),
    adherence_rate: toNumber(updated.adherence_rate),
    total_checks: Number(updated.total_checks ?? 0),
    created_at: String(updated.created_at ?? new Date().toISOString()),
    updated_at: String(updated.updated_at ?? updated.created_at ?? new Date().toISOString()),
  };
}

function mapInsightKind(value: unknown): JournalInsight["kind"] {
  const normalized = String(value ?? "pattern");
  if (normalized === "aggregate") return "milestone";
  if (normalized === "review_queue") return "anomaly";
  if (normalized === "calendar_day") return "streak";
  return "pattern";
}

export async function fetchInsights(): Promise<JournalInsight[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>("/api/journal/insights");
  return (response.items ?? []).map((item, index) => ({
    id: String(item.id ?? `${item.type ?? "insight"}-${index}`),
    kind: mapInsightKind(item.type),
    title: String(item.title ?? "Insight"),
    description: String(item.summary ?? ""),
    relevance_score: null,
    created_at: String(item.timestamp ?? new Date().toISOString()),
    related_run_ids: [],
    related_rule_ids: [],
  }));
}

export async function fetchJournalEnvironments(): Promise<JournalEnvironment[]> {
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>("/api/journal/v2/environments");
  return (response.items ?? []).map(normalizeJournalEnvironment);
}

export async function fetchJournalEpisodes(params: { environment_id: string; limit?: number; offset?: number }): Promise<JournalEpisode[]> {
  const environmentId = requireEnvironmentId(params.environment_id, "fetchJournalEpisodes");
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/v2/episodes${toSearchParams({
      environment_id: environmentId,
      limit: params.limit != null ? String(params.limit) : undefined,
      offset: params.offset != null ? String(params.offset) : undefined,
    })}`,
  );
  return (response.items ?? []).map(normalizeJournalEpisode);
}

export async function fetchJournalV2AnalyticsSummary(environment_id: string): Promise<JournalV2AnalyticsSummary> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalV2AnalyticsSummary");
  return apiFetch<JournalV2AnalyticsSummary>(
    `/api/journal/v2/analytics/summary${toSearchParams({ environment_id: environmentId })}`,
  );
}

export async function fetchJournalV2AnalyticsStrategies(environment_id: string): Promise<JournalV2StrategyAnalytics> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalV2AnalyticsStrategies");
  return apiFetch<JournalV2StrategyAnalytics>(
    `/api/journal/v2/analytics/strategies${toSearchParams({ environment_id: environmentId })}`,
  );
}

export async function fetchJournalV2PaperLiveComparison(params: {
  template_id: string;
  paper_environment_id: string;
  live_environment_id: string;
}): Promise<JournalV2PaperLiveComparison> {
  const templateId = String(params.template_id ?? "").trim();
  if (!templateId) {
    throw new Error("fetchJournalV2PaperLiveComparison requires template_id");
  }
  const paperEnvironmentId = requireEnvironmentId(params.paper_environment_id, "fetchJournalV2PaperLiveComparison paper_environment_id");
  const liveEnvironmentId = requireEnvironmentId(params.live_environment_id, "fetchJournalV2PaperLiveComparison live_environment_id");
  return apiFetch<JournalV2PaperLiveComparison>(
    `/api/journal/v2/analytics/compare-paper-live${toSearchParams({
      template_id: templateId,
      paper_environment_id: paperEnvironmentId,
      live_environment_id: liveEnvironmentId,
    })}`,
  );
}

export async function fetchJournalV2Unresolved(environment_id: string): Promise<JournalV2UnresolvedQueue> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalV2Unresolved");
  return apiFetch<JournalV2UnresolvedQueue>(
    `/api/journal/v2/unresolved${toSearchParams({ environment_id: environmentId })}`,
  );
}

export async function fetchJournalEpisode(episodeId: string, environment_id: string): Promise<JournalEpisode> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalEpisode");
  const response = await apiFetch<Record<string, unknown>>(
    `/api/journal/v2/episodes/${episodeId}${toSearchParams({ environment_id: environmentId })}`,
  );
  return normalizeJournalEpisode(response);
}

export async function fetchJournalTimeline(episodeId: string, environment_id: string): Promise<JournalTimelineEvent[]> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalTimeline");
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/v2/episodes/${episodeId}/timeline${toSearchParams({ environment_id: environmentId })}`,
  );
  return (response.items ?? []).map(normalizeJournalTimelineEvent);
}

export async function fetchJournalNotes(params: JournalNotesQuery): Promise<JournalNote[]> {
  const environmentId = requireEnvironmentId(params.environment_id, "fetchJournalNotes");
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/v2/notes${toSearchParams({
      environment_id: environmentId,
      subject_type: params.subject_type,
      subject_id: params.subject_id,
      episode_id: params.episode_id,
      note_type: params.note_type,
      limit: params.limit != null ? String(params.limit) : undefined,
      offset: params.offset != null ? String(params.offset) : undefined,
    })}`,
  );
  return (response.items ?? []).map(normalizeJournalNote);
}

export async function fetchJournalNoteRevisions(noteId: string, environment_id: string): Promise<JournalNoteRevision[]> {
  const environmentId = requireEnvironmentId(environment_id, "fetchJournalNoteRevisions");
  const response = await apiFetch<{ items?: Array<Record<string, unknown>> }>(
    `/api/journal/v2/notes/${noteId}/revisions${toSearchParams({ environment_id: environmentId })}`,
  );
  return (response.items ?? []).map((item) => ({
    note_id: String(item.note_id ?? noteId),
    revision_no: Number(item.revision_no ?? 0),
    body_markdown: String(item.body_markdown ?? ""),
    body_text: item.body_text != null ? String(item.body_text) : undefined,
    edited_at: item.edited_at != null ? String(item.edited_at) : undefined,
    change_reason: item.change_reason != null ? String(item.change_reason) : null,
  }));
}

export async function createJournalNote(payload: CreateJournalNotePayload): Promise<JournalNote> {
  requireEnvironmentId(payload.environment_id, "createJournalNote");
  const response = await apiFetch<Record<string, unknown>>("/api/journal/v2/notes", {
    method: "POST",
    json: payload,
  });
  return normalizeJournalNote(response);
}

export async function updateJournalNote(noteId: string, payload: UpdateJournalNotePayload): Promise<JournalNote> {
  requireEnvironmentId(payload.environment_id, "updateJournalNote");
  const response = await apiFetch<Record<string, unknown>>(`/api/journal/v2/notes/${noteId}`, {
    method: "PATCH",
    json: payload,
  });
  return normalizeJournalNote(response);
}
