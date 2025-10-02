/**
 * Centralized API base URL resolution and session management.
 * - getApiBase: resolves backend base URL for cross-device access
 * - setSessionId/getSessionId/clearSessionId: manage session for header fallback
 * - apiFetch: adds credentials and X-Session-ID header automatically
 */

const SESSION_STORAGE_KEY = 'kite_session_id';

export function getApiBase(): string {
	// Returns the base URL for the API, e.g., "http://localhost:8777".
	// It does NOT include the "/api" suffix. Callers should append it.
	const env = (import.meta as any).env?.VITE_API_BASE_URL as string | undefined;
	if (env && typeof env === 'string' && env.trim().length > 0) {
		return env.replace(/\/+$/, '');
	}
	if (typeof window !== 'undefined') {
		const proto = window.location.protocol || 'http:';
		const host = window.location.hostname || 'localhost';
		const port = '8777';
		return `${proto}//${host}:${port}`;
	}
	// Fallback for SSR or unknown environment
	return 'http://localhost:8777';
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

export async function getUserSubscriptions(scope?: 'sidebar' | 'marketwatch') {
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : '';
    const response = await apiFetch(`/user/subscriptions${qs}`);
    if (!response.ok) {
        throw new Error('Failed to fetch user subscriptions');
    }
    return response.json();
}

export async function saveUserSubscriptions(subscriptions: any, scope?: 'sidebar' | 'marketwatch') {
    const qs = scope ? `?scope=${encodeURIComponent(scope)}` : '';
    const response = await apiFetch(`/user/subscriptions${qs}`, {
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
  const res = await apiFetch('/api/options/sessions');
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
  const res = await apiFetch(`/alerts${qs}`);
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Failed to fetch alerts: ${res.status} ${t}`);
  }
  return (await res.json()) as ListAlertsResponse;
}

export async function createAlert(body: AlertCreateRequest): Promise<Alert> {
  const res = await apiFetch('/alerts', {
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
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}`, {
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
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}${qs}`, { method: 'DELETE' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Delete alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function duplicateAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}/duplicate`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Duplicate alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function pauseAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}/pause`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Pause alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function resumeAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}/resume`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Resume alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function cancelAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`Cancel alert failed: ${res.status} ${t}`);
  }
  return (await res.json()) as Alert;
}

export async function reactivateAlert(id: string): Promise<Alert> {
  const res = await apiFetch(`/alerts/${encodeURIComponent(id)}/reactivate`, { method: 'POST' });
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
  const res = await apiFetch('/api/options/sessions', {
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
  const res = await apiFetch(`/api/options/session/${encodeURIComponent(underlying)}`);
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
  const res = await apiFetch(`/api/options/chain/${encodeURIComponent(underlyingSymbol)}`);
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
  const res = await apiFetch(`/api/options/session/${encodeURIComponent(underlying)}`, {
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
  const base = getApiBase(); // e.g., http://localhost:8777
  const wsProto = base.startsWith('https') ? 'wss' : 'ws';
  const wsBase = base.replace(/^http/, wsProto);
  return `${wsBase}/ws/options/session/${encodeURIComponent(underlying)}`;
}

/**
 * Connect to WS /ws/options/session/{underlying}
 * Messages are raw OptionsSessionSnapshot JSON documents.
 */
export function openOptionsSessionWS(underlying: string): WebSocket {
  const loc = window.location;
  const isSecure = loc.protocol === 'https:';
  const scheme = isSecure ? 'wss' : 'ws';
  const apiBase = getApiBase();
  const basePath = apiBase.replace(/^https?:\/\/[^/]+/, '');
  const url = `${scheme}://${loc.host}${basePath}/api/ws/options/session/${encodeURIComponent(
    underlying
  )}`;
  return new WebSocket(url);
}
