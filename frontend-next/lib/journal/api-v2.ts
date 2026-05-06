import { apiFetch } from "@/lib/api/client";
import type {
  JournalV2DailyResponse,
  JournalV2EpisodeDetailResponse,
  JournalV2PeriodResponse,
  JournalV2StrategyListResponse,
  MetricPeriod,
} from "./types-v2";

// Re-export MetricPeriod for consumer convenience
export type { MetricPeriod };

// ---------------------------------------------------------------------------
// Internal helpers (mirrors the existing api.ts style)
// ---------------------------------------------------------------------------

function toSearchParams(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter((e): e is [string, string] => e[1] !== undefined);
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : "";
}

// ---------------------------------------------------------------------------
// Journal v2 helpers
// ---------------------------------------------------------------------------

/** GET /api/journal/v2/daily */
export async function fetchDailyView(params: {
  environment_id: string;
  date?: string;
}): Promise<JournalV2DailyResponse> {
  return apiFetch<JournalV2DailyResponse>(
    `/api/journal/v2/daily${toSearchParams({
      environment_id: params.environment_id,
      date: params.date,
    })}`,
  );
}

/** GET /api/journal/v2/period */
export async function fetchPeriodView(params: {
  environment_id: string;
  from: string;
  to: string;
  granularity?: string;
}): Promise<JournalV2PeriodResponse> {
  return apiFetch<JournalV2PeriodResponse>(
    `/api/journal/v2/period${toSearchParams({
      environment_id: params.environment_id,
      from: params.from,
      to: params.to,
      granularity: params.granularity,
    })}`,
  );
}

/** GET /api/journal/v2/episodes/{episode_id} */
export async function fetchEpisodeDetail(params: {
  environment_id: string;
  episode_id: string;
}): Promise<JournalV2EpisodeDetailResponse> {
  return apiFetch<JournalV2EpisodeDetailResponse>(
    `/api/journal/v2/episodes/${params.episode_id}${toSearchParams({
      environment_id: params.environment_id,
    })}`,
  );
}

/** GET /api/journal/v2/strategies */
export async function fetchJournalStrategies(params: {
  environment_id: string;
  period?: MetricPeriod;
  date?: string;
}): Promise<JournalV2StrategyListResponse> {
  return apiFetch<JournalV2StrategyListResponse>(
    `/api/journal/v2/strategies${toSearchParams({
      environment_id: params.environment_id,
      period: params.period,
      date: params.date,
    })}`,
  );
}

/** PATCH /api/journal/v2/episodes/{episode_id} */
export async function patchEpisodeNotes(params: {
  environment_id: string;
  episode_id: string;
  notes: string;
}): Promise<JournalV2EpisodeDetailResponse> {
  return apiFetch<JournalV2EpisodeDetailResponse>(
    `/api/journal/v2/episodes/${params.episode_id}${toSearchParams({
      environment_id: params.environment_id,
    })}`,
    {
      method: "PATCH",
      json: { notes: params.notes },
    },
  );
}
