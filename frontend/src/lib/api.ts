/**
 * Centralized API base URL resolution and session management.
 * - getApiBase: resolves backend base URL for cross-device access
 * - setSessionId/getSessionId/clearSessionId: manage session for header fallback
 * - apiFetch: adds credentials and X-Session-ID header automatically
 */

const SESSION_STORAGE_KEY = 'kite_session_id';

export function getApiBase(): string {
    // 1) Explicit override via env (for cross-device dev access)
    const env = (import.meta as any).env?.VITE_API_BASE_URL as string | undefined;
    if (env && typeof env === 'string' && env.trim().length > 0) {
        return env.replace(/\/+$/, '');
    }
    
    // 2) Always use relative URLs (empty string) in both browser and SSR
    //    - Dev: Vite proxy handles /broker -> localhost:8777
    //    - Prod: Caddy reverse proxy handles /broker -> backend container
    return '';
}

export function setSessionId(id: string | null | undefined) {
	try {
		if (typeof window === 'undefined') return;
		if (id && id.trim().length > 0) {
			window.localStorage.setItem(SESSION_STORAGE_KEY, id);
		} else {
			window.localStorage.removeItem(SESSION_STORAGE_KEY);
		}
	} catch {
		// ignore storage errors
	}
}

export function getSessionId(): string | null {
	try {
		if (typeof window === 'undefined') return null;
		return window.localStorage.getItem(SESSION_STORAGE_KEY);
	} catch {
		return null;
	}
}

export function clearSessionId() {
	setSessionId(null);
}

export async function apiFetch(path: string, init: RequestInit = {}) {
	const url = `${getApiBase()}${path.startsWith('/') ? path : `/${path}`}`;
	const headers = new Headers(init.headers || {});
	const sid = getSessionId();
	if (sid && !headers.has('X-Session-ID')) {
		headers.set('X-Session-ID', sid);
	}
	const opts: RequestInit = {
		credentials: 'include',
		...init,
		headers
	};
	return fetch(url, opts);
}

/**
 * One-shot LTP helper. Returns last_price or null.
 * Uses existing backend route POST /broker/ltp with { instruments: ["EX:TS"] }.
 */
export async function getLtp(exchange: string, tradingsymbol: string): Promise<number | null> {
	try {
		const key = `${exchange}:${tradingsymbol}`;
		const res = await apiFetch('/broker/ltp', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ instruments: [key] })
		});
		if (!res.ok) return null;
		const data = await res.json().catch(() => null) as any;
		const rec = data?.[key] ?? (data?.data ? data.data[key] : null);
		const price = rec?.last_price ?? rec?.lastPrice ?? null;
		if (typeof price === 'number') return price;
		if (price != null) {
			const n = Number(price);
			return Number.isFinite(n) ? n : null;
		}
		return null;
	} catch {
		return null;
	}
}

export async function getUserSubscriptions(scope?: 'sidebar' | 'marketwatch' | 'nfo-charts' | 'nfo-charts-layouts') {
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : '';
    const response = await apiFetch(`/broker/user/subscriptions${qs}`);
    if (!response.ok) {
        throw new Error('Failed to fetch user subscriptions');
    }
    return response.json();
}

export async function saveUserSubscriptions(subscriptions: any, scope?: 'sidebar' | 'marketwatch' | 'nfo-charts' | 'nfo-charts-layouts') {
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : '';
    const response = await apiFetch(`/broker/user/subscriptions${qs}`, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(subscriptions)
    });
    if (!response.ok) {
        throw new Error('Failed to save user subscriptions');
    }
    return response.json();
}

/**
 * Alerts API
 */
import type {
  Alert,
  ListAlertsResponse,
  AlertCreateRequest,
  AlertPatchRequest,
  SessionsRequest,
  WatchlistItem,
  OptionsSessionSnapshot,
  StopSessionResponse,
  ErrorResponse,
  SessionRequestItem
} from '$lib/types';

function toQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    usp.set(k, String(v));
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : '';
}

export async function getOptionsWatchlist(): Promise<WatchlistItem[]> {
  const res = await apiFetch('/broker/options/sessions');
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`getOptionsWatchlist failed: ${res.status} ${t}`);
  }
  return (await res.json()) as WatchlistItem[];
}

export async function getAlerts(params: {
  status?: string;
  instrument_token?: number;
  instrument_name?: string;
  limit?: number;
  offset?: number;
  sort?: 'created_at' | '-created_at' | 'updated_at' | '-updated_at';
} = {}): Promise<ListAlertsResponse> {
  const newParams: any = {...params};
  if (newParams.instrument_token) {
    newParams.instrument_name = newParams.instrument_token;
    delete newParams.instrument_token;
  }
  const qs = toQuery(newParams);
  const res = await apiFetch(`/broker/alerts${qs}`);
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Failed to fetch alerts: ${res.status} ${t}`);
  }
  return (await res.json()) as ListAlertsResponse;
}

export async function createAlert(body: AlertCreateRequest): Promise<Alert> {
  const res = await apiFetch('/broker/alerts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (res.status === 424) {
    // Baseline unavailable special-case for UX
    const detail = await res.json().catch(() => null) as any;
    const err = new Error(detail?.detail ?? 'Baseline price unavailable. Provide baseline_price or try later.');
    (err as any).status = 424;
    throw err;
  }
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Create alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function patchAlert(id: string, patch: AlertPatchRequest): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch)
  });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Patch alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function deleteAlert(id: string, hard?: boolean): Promise<Alert> {
  const qs = toQuery({ hard: !!hard });
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}${qs}`, { method: 'DELETE' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Delete alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function duplicateAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}/duplicate`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Duplicate alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function pauseAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}/pause`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Pause alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function resumeAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}/resume`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Resume alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function cancelAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Cancel alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function reactivateAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/broker/alerts/${encodeURIComponent(id)}/reactivate`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Reactivate alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

/**
 * Options API
 * Typed HTTP wrappers for sessions and snapshots.
 */

/**
 * POST /options/sessions — start/update/replace sessions
 */
export async function postOptionsSessions(payload: SessionsRequest): Promise<WatchlistItem[]> {
  const res = await apiFetch('/broker/options/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`postOptionsSessions failed: ${res.status} ${t}`);
  }
  return (await res.json()) as WatchlistItem[];
}

export async function startOptionsSessions(
  items: SessionRequestItem[],
  replace = false
): Promise<WatchlistItem[]> {
  return postOptionsSessions({ items, replace });
}

/**
 * GET /options/session/{underlying}
 * Returns the latest snapshot. Throws error with status=404 if no active session.
 */
export async function getOptionsSnapshot(underlying: string): Promise<OptionsSessionSnapshot> {
  const res = await apiFetch(`/broker/options/session/${encodeURIComponent(underlying)}`);
  if (res.status === 404) {
    const body = await res.json().catch(() => ({}));
    const detail = (body?.detail ?? body) as ErrorResponse | string;
    if (typeof detail === 'object' && detail?.code === 'OPTION_SESSION_NOT_FOUND') {
      throw detail;
    }
  }
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`getOptionsSnapshot failed: ${res.status} ${t}`);
  }
  return (await res.json()) as OptionsSessionSnapshot;
}

/**
 * GET /options/chain/{underlying_symbol} — alias of snapshot
 * Throws error with status=404 if no active session.
 */
export async function getOptionChain(underlyingSymbol: string): Promise<OptionsSessionSnapshot> {
  const res = await apiFetch(`/broker/options/chain/${encodeURIComponent(underlyingSymbol)}`);
  if (res.status === 404) {
    const detail = (await res.json().catch(() => null)) as any;
    const err = new Error(detail?.detail ?? 'No active options session');
    (err as any).status = 404;
    throw err;
  }
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`getOptionChain failed: ${res.status} ${t}`);
  }
  return (await res.json()) as OptionsSessionSnapshot;
}

/**
 * DELETE /options/session/{underlying}
 * Note: keep leading slash to avoid backend decorator missing-slash quirk.
 */
export async function deleteOptionsSession(underlying: string): Promise<StopSessionResponse> {
  const res = await apiFetch(`/broker/options/session/${encodeURIComponent(underlying)}`, {
    method: 'DELETE'
  });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`deleteOptionsSession failed: ${res.status} ${t}`);
  }
  return (await res.json()) as StopSessionResponse;
}

export async function stopOptionsSession(underlying: string): Promise<StopSessionResponse> {
  return deleteOptionsSession(underlying);
}

/**
 * Build WS URL for options session stream.
 * Transforms http(s) base to ws(s) and appends /ws/options/session/{underlying}.
 */
export function buildOptionsSessionWsUrl(underlying: string): string {
  const base = getApiBase();
  // If base is empty or relative, derive from window.location
  if (!base || base.startsWith('/')) {
    const loc = typeof window !== 'undefined' ? window.location : ({ protocol: 'http:', host: 'localhost:8777' } as any);
    const wsProto = loc.protocol === 'https:' ? 'wss' : 'ws';
    return `${wsProto}://${loc.host}/broker/ws/options/session/${encodeURIComponent(underlying)}`;
  }
  const wsProto = base.startsWith('https') ? 'wss' : 'ws';
  const wsHost = base.replace(/^https?:\/\//, '');
  return `${wsProto}://${wsHost}/broker/ws/options/session/${encodeURIComponent(underlying)}`;
}
/**
 * Build SSE URL for options session stream.
 */
export function buildOptionsSessionSseUrl(underlying: string): string {
  const base = getApiBase(); // e.g., http://localhost:8777
  return `${base}/broker/sse/options/session/${encodeURIComponent(underlying)}`;
}

// ═══════════════════════════════════════════════════════════════════════════════
// WEBHOOK EVENTS API
// ═══════════════════════════════════════════════════════════════════════════════

export interface WebhookEvent {
  id: string;
  order_id: string;
  user_id: string;
  status: string;
  event_timestamp: string;
  received_at: string;
  exchange: string | null;
  tradingsymbol: string | null;
  instrument_token: number | null;
  transaction_type: string | null;
  quantity: number | null;
  filled_quantity: number | null;
  average_price: number | null;
  payload: Record<string, any>;
}

export interface WebhookEventsResponse {
  events: WebhookEvent[];
  total?: number;
}

export interface WebhookEventsFilters {
  order_id?: string;
  user_id?: string;
  status?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
  offset?: number;
}

/**
 * Fetch webhook events with optional filters
 */
export async function getWebhookEvents(filters?: WebhookEventsFilters): Promise<WebhookEvent[]> {
  const params = new URLSearchParams();
  
  if (filters?.order_id) params.append('order_id', filters.order_id);
  if (filters?.user_id) params.append('user_id', filters.user_id);
  if (filters?.status) params.append('status', filters.status);
  if (filters?.start_date) params.append('start_date', filters.start_date);
  if (filters?.end_date) params.append('end_date', filters.end_date);
  if (filters?.limit) params.append('limit', filters.limit.toString());
  if (filters?.offset) params.append('offset', filters.offset.toString());
  
  const qs = params.toString();
  const path = `/broker/webhooks/orders/events${qs ? `?${qs}` : ''}`;
  const resp = await apiFetch(path);
  if (!resp.ok) {
    throw new Error(`Failed to fetch webhook events: ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Connect to WS /ws/options/session/{underlying}
 * Messages are raw OptionsSessionSnapshot JSON documents.
 */
export function openOptionsSessionWS(underlying: string): WebSocket {
  const url = buildOptionsSessionWsUrl(underlying);
  return new WebSocket(url);
}


/**
 * Historical Candles API
 */

export interface Candle {
	time: number; // UTC epoch seconds
	open: number;
	high: number;
	low: number;
	close: number;
	volume: number;
	oi?: number;
}

export interface CandlesResponse {
	status: 'success';
	meta: {
		instrument_token: number;
		interval: string;
		timezone: string; // 'UTC' | 'Asia/Kolkata'
		from: string;
		to: string;
		count: number;
	};
	ingestion: {
		status: 'triggered' | 'up_to_date' | 'disabled' | 'error';
		message?: string;
	};
	candles: Candle[];
}

type CanonicalTimeframe =
	| 'minute'
	| '3minute'
	| '5minute'
	| '10minute'
	| '15minute'
	| '30minute'
	| '60minute'
	| 'day';

// MUST match backend's TIMEFRAME_ALIASES in candles_api.py
const TIMEFRAME_ALIASES: Record<string, CanonicalTimeframe> = {
	'1m': 'minute',
	min: 'minute',
	minute: 'minute',
	'3m': '3minute',
	'3minute': '3minute',
	'5m': '5minute',
	'5minute': '5minute',
	'10m': '10minute',
	'10minute': '10minute',
	'15m': '15minute',
	'15minute': '15minute',
	'30m': '30minute',
	'30minute': '30minute',
	'60m': '60minute',
	'1h': '60minute',
	'60minute': '60minute',
	'1d': 'day',
	day: 'day'
};

export function normalizeTimeframe(timeframe: string): CanonicalTimeframe {
	const normalized = TIMEFRAME_ALIASES[timeframe.toLowerCase()];
	if (!normalized) {
		// Fallback for existing values that might not be in the alias map
		const validTimeframes: string[] = [
			'minute',
			'3minute',
			'5minute',
			'10minute',
			'15minute',
			'30minute',
			'60minute',
			'day'
		];
		if (validTimeframes.includes(timeframe)) return timeframe as CanonicalTimeframe;
		throw new Error(`Invalid timeframe alias: "${timeframe}"`);
	}
	return normalized;
}

export async function fetchCandles(
	identifier: string | number,
	opts: {
		timeframe: string;
		from?: string | number | Date;
		to?: string | number | Date;
		ingest?: boolean;
	}
): Promise<CandlesResponse> {
	const canonicalTimeframe = normalizeTimeframe(opts.timeframe);
	const params = new URLSearchParams({ timeframe: canonicalTimeframe });

	if (opts.from) {
		if (opts.from instanceof Date) {
			params.set('from', opts.from.toISOString());
		} else if (typeof opts.from === 'number') {
			params.set('from', new Date(opts.from * 1000).toISOString());
		} else {
			params.set('from', opts.from);
		}
	}

	if (opts.to) {
		if (opts.to instanceof Date) {
			params.set('to', opts.to.toISOString());
		} else if (typeof opts.to === 'number') {
			params.set('to', new Date(opts.to * 1000).toISOString());
		} else {
			params.set('to', opts.to);
		}
	}

	// Default ingest to true if not specified
	params.set('ingest', opts.ingest === false ? 'false' : 'true');

	const url = `/broker/candles/${identifier}?${params.toString()}`;
	const res = await apiFetch(url);

	if (!res.ok) {
		const errorText = await res.text().catch(() => 'Unknown error');
		throw new Error(`Failed to fetch candles: ${res.status} ${errorText}`);
	}

	const data = await res.json();

	// Ensure numeric types are correct
	const candles = data.candles.map((c: any) => ({
		time: Number(c.time),
		open: Number(c.open),
		high: Number(c.high),
		low: Number(c.low),
		close: Number(c.close),
		volume: Number(c.volume),
		oi: c.oi ? Number(c.oi) : undefined
	}));

	return { ...data, candles };
}

export async function clearCandleCache(
	identifier: string | number
): Promise<{ status: string; deleted_rows: number; instrument_token: number }> {
	const url = `/broker/candles/${identifier}/cache`;
	const res = await apiFetch(url, { method: 'DELETE' });

	if (!res.ok) {
		const errorText = await res.text().catch(() => 'Unknown error');
		throw new Error(`Failed to clear candle cache: ${res.status} ${errorText}`);
	}

	return res.json();
}

/**
 * Get data coverage statistics for an instrument
 */
export async function getCandleCoverage(
	identifier: string | number,
	timeframe: string
): Promise<any> {
	const canonicalTimeframe = normalizeTimeframe(timeframe);
	const url = `/broker/candles/${identifier}/coverage?timeframe=${canonicalTimeframe}`;
	const res = await apiFetch(url);

	if (!res.ok) {
		const errorText = await res.text().catch(() => 'Unknown error');
		throw new Error(`Failed to get candle coverage: ${res.status} ${errorText}`);
	}

	return res.json();
}

/**
 * Build SSE URL for real-time candle streaming
 */
export function buildCandleStreamUrl(identifier: string | number, timeframe: string): string {
	const canonicalTimeframe = normalizeTimeframe(timeframe);
	const base = getApiBase();
	return `${base}/broker/candles/stream/${identifier}?timeframe=${canonicalTimeframe}`;
}

/**
 * Watchlist API - Dedicated endpoints for managing user watchlists
 */

export interface WatchlistInstrument {
	instrument_token: number;
	tradingsymbol?: string;
	name?: string;
	exchange?: string;
	instrument_type?: string;
}

export interface WatchlistUpsertResponse {
	inserted: number;
	updated: number;
	removed: number;
}

/**
 * Get user's watchlist
 */
export async function getUserWatchlist(ownerId: string = 'default'): Promise<WatchlistInstrument[]> {
	const url = `/broker/candles/user/watchlist?owner_id=${encodeURIComponent(ownerId)}`;
	const res = await apiFetch(url);
	
	if (!res.ok) {
		const errorText = await res.text().catch(() => 'Unknown error');
		throw new Error(`Failed to fetch watchlist: ${res.status} ${errorText}`);
	}
	
	return res.json();
}

/**
 * Upsert instruments to user's watchlist
 * @param instruments - List of instruments to add/update
 * @param ownerId - Owner ID (defaults to 'default')
 * @param replace - If true, removes instruments not in the list
 */
export async function upsertUserWatchlist(
	instruments: WatchlistInstrument[],
	ownerId: string = 'default',
	replace: boolean = false
): Promise<WatchlistUpsertResponse> {
	const url = `/broker/candles/user/watchlist?replace=${replace}`;
	const res = await apiFetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			owner_id: ownerId,
			instruments
		})
	});
	
	if (!res.ok) {
		const errorText = await res.text().catch(() => 'Unknown error');
		throw new Error(`Failed to upsert watchlist: ${res.status} ${errorText}`);
	}
	
	return res.json();
}
