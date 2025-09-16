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
   headers,
 };
 return fetch(url, opts);
}