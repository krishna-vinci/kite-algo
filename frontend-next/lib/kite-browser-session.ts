const KITE_BROWSER_SESSION_HINT_KEY = "kite_browser_session_ready";
const KITE_BROWSER_SESSION_EVENT = "kite-browser-session-change";

function notify() {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new Event(KITE_BROWSER_SESSION_EVENT));
}

export function hasKiteBrowserSessionHint() {
  if (typeof window === "undefined") {
    return false;
  }
  return window.localStorage.getItem(KITE_BROWSER_SESSION_HINT_KEY) === "1";
}

export function markKiteBrowserSessionHint() {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(KITE_BROWSER_SESSION_HINT_KEY, "1");
  notify();
}

export function clearKiteBrowserSessionHint() {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(KITE_BROWSER_SESSION_HINT_KEY);
  notify();
}

export function onKiteBrowserSessionHintChange(callback: () => void) {
  if (typeof window === "undefined") {
    return () => undefined;
  }
  window.addEventListener(KITE_BROWSER_SESSION_EVENT, callback);
  return () => window.removeEventListener(KITE_BROWSER_SESSION_EVENT, callback);
}
