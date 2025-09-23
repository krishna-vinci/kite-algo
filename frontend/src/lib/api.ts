/**
 * Centralized API base URL resolution and session management.
 * - getApiBase: resolves backend base URL for cross-device access
 * - setSessionId/getSessionId/clearSessionId: manage session for header fallback
 * - apiFetch: adds credentials and X-Session-ID header automatically
 */

const SESSION_STORAGE_KEY = 'kite_session_id';

export function getApiBase(): string {
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
