/**
 * Typed wrappers over the option market-data API (`/api/options/*`,
 * `backend/options/api/market_router.py`). One function per endpoint used by
 * the Options page.
 */

import { ApiClientError, apiFetch } from "@/lib/api/client";
import type {
  OptionAnalyticValue,
  OptionChain,
  OptionExpiries,
  OptionSession,
  StartOptionSessionsPayload,
  StartOptionSessionsResponse,
} from "@/lib/options/types";

const BASE = "/api/options";

export async function startOptionSessions(
  payload: StartOptionSessionsPayload,
): Promise<StartOptionSessionsResponse> {
  return apiFetch<StartOptionSessionsResponse>(`${BASE}/sessions`, {
    method: "POST",
    json: payload,
  });
}

/** Returns `null` when no session is active yet (404), so callers can start one. */
export async function fetchOptionSession(underlying: string): Promise<OptionSession | null> {
  try {
    return await apiFetch<OptionSession>(`${BASE}/underlyings/${encodeURIComponent(underlying)}/session`);
  } catch (error) {
    if (error instanceof ApiClientError && error.status === 404) return null;
    throw error;
  }
}

export async function fetchOptionExpiries(underlying: string): Promise<OptionExpiries> {
  return apiFetch<OptionExpiries>(`${BASE}/underlyings/${encodeURIComponent(underlying)}/expiries`);
}

export async function fetchOptionChain(underlying: string, expiry: string): Promise<OptionChain> {
  const params = new URLSearchParams({ expiry });
  return apiFetch<OptionChain>(
    `${BASE}/underlyings/${encodeURIComponent(underlying)}/chain?${params.toString()}`,
  );
}

export async function fetchOptionPcr(underlying: string, expiry: string): Promise<OptionAnalyticValue> {
  const params = new URLSearchParams({ expiry });
  return apiFetch<OptionAnalyticValue>(
    `${BASE}/underlyings/${encodeURIComponent(underlying)}/analytics/pcr?${params.toString()}`,
  );
}

export async function fetchOptionMaxPain(underlying: string, expiry: string): Promise<OptionAnalyticValue> {
  const params = new URLSearchParams({ expiry });
  return apiFetch<OptionAnalyticValue>(
    `${BASE}/underlyings/${encodeURIComponent(underlying)}/analytics/max-pain?${params.toString()}`,
  );
}
