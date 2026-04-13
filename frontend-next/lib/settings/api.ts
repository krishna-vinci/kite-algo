import { apiFetch } from "@/lib/api/client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type IndexSourceList = "Nifty50" | "NiftyBank" | "Nifty500";

export type IndexRefreshState = {
  source_list: string;
  last_constituent_refresh_at: string | null;
  last_live_refresh_at: string | null;
  added_symbols: string[];
  removed_symbols: string[];
  needs_review: boolean;
  pending_review_count: number;
  last_error: string | null;
  updated_at: string | null;
};

export type IndexConstituentRow = {
  tradingsymbol: string;
  company_name: string | null;
  sector: string | null;
  instrument_token: number | null;
  exchange: string | null;
  source_list: string;
  index_weight: number | null;
  baseline_index_weight: number | null;
  freefloat_marketcap: number | null;
  baseline_freefloat_marketcap: number | null;
  ltp: number | null;
  net_change_percent: number | null;
  return_attribution: number | null;
  needs_weight_review: boolean;
  baseline_as_of_date: string | null;
  last_refreshed_at: string | null;
};

export type BaselineEntry = {
  symbol: string;
  weight: number;
};

export type BaselineSavePayload = {
  entries: BaselineEntry[];
  force?: boolean;
  normalized_total?: number;
  normalize_freefloat?: boolean;
};

type ApiResultMessage = {
  status: string;
  message?: string;
  [key: string]: unknown;
};

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export async function fetchIndexStatus(sourceList: IndexSourceList): Promise<IndexRefreshState> {
  return apiFetch<IndexRefreshState>(`/api/indices/${sourceList}/status`);
}

export async function fetchIndexConstituents(sourceList: IndexSourceList): Promise<IndexConstituentRow[]> {
  const grouped = await apiFetch<Record<string, IndexConstituentRow[]>>(`/api/indices/${sourceList}`);
  return Object.values(grouped).flat();
}

export async function refreshConstituents(sourceLists?: IndexSourceList[]): Promise<ApiResultMessage> {
  const params = sourceLists?.length
    ? `?${sourceLists.map((s) => `source_list=${encodeURIComponent(s)}`).join("&")}`
    : "";
  return apiFetch<ApiResultMessage>(`/api/indices/refresh${params}`, { method: "POST" });
}

export async function refreshLiveMetrics(includeNifty500 = false): Promise<ApiResultMessage> {
  return apiFetch<ApiResultMessage>(
    `/api/update-nifty50-data${includeNifty500 ? "?include_nifty500=true" : ""}`,
    { method: "POST" },
  );
}

export async function seedDefaultBaseline(sourceList: "Nifty50" | "NiftyBank"): Promise<ApiResultMessage> {
  const slug = sourceList === "Nifty50" ? "nifty50" : "niftybank";
  return apiFetch<ApiResultMessage>(`/api/indices/${slug}/baseline/default`, { method: "POST" });
}

export async function saveBaseline(
  sourceList: IndexSourceList,
  payload: BaselineSavePayload,
): Promise<ApiResultMessage> {
  return apiFetch<ApiResultMessage>(`/api/indices/${sourceList}/baseline`, {
    method: "POST",
    json: payload,
  });
}
