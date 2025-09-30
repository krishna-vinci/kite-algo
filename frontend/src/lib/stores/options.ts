import { writable } from 'svelte/store';
import { browser } from '$app/environment';
import {
  postOptionsSessions,
  getOptionsSession,
  deleteOptionsSession,
  connectOptionsSessionWS
} from '$lib/api';
import type {
  SessionRequestItem,
  WatchlistItem,
  OptionsSessionSnapshot,
  StopSessionResponse
} from '$lib/types';

/**
 * Options session store
 * - Manages watchlist, selected underlying/expiry
 * - Fetches snapshots via GET /options/session/{underlying} and streams via WS /ws/options/session/{underlying}
 * - Handles WS lifecycle with exponential backoff + fallback polling
 */

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'error';

export interface OptionsStoreState {
  selectedUnderlying: string | null;
  selectedExpiry: string | null;
  watchlist: WatchlistItem[];
  latestSnapshot: OptionsSessionSnapshot | null;
  connectionStatus: ConnectionStatus;
  errors: { last?: string } | null;
  config: { window: number; cadence_sec: number; replace: boolean };
  polling: { active: boolean; intervalMs: number };
}

function createOptionsStore() {
  const { subscribe, update, set } = writable<OptionsStoreState>({
    selectedUnderlying: null,
    selectedExpiry: null,
    watchlist: [],
    latestSnapshot: null,
    connectionStatus: 'idle',
    errors: null,
    config: { window: 12, cadence_sec: 5, replace: false },
    polling: { active: false, intervalMs: 5000 }
  });

  // Private WS/polling state
  let ws: WebSocket | null = null;
  let wsUnderlying: string | null = null;
  let reconnectAttempts = 0;
  let reconnectTimer: number | null = null;
  let pollingTimer: number | null = null;
  let manualDisconnect = false;

  // ----- Helpers -----

  /** Compute exponential backoff in ms: 1s,2s,4s,8s,max 10s */
  function computeBackoffMs(attempt: number): number {
    const secs = Math.min(10, Math.pow(2, Math.max(0, attempt - 1)));
    return secs * 1000;
  }

  function clearReconnectTimer() {
    if (reconnectTimer != null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function stopPolling() {
    if (pollingTimer != null) {
      clearInterval(pollingTimer);
      pollingTimer = null;
    }
    update((s) => ({
      ...s,
      polling: { ...s.polling, active: false }
    }));
  }

  function startPolling(underlying: string, intervalMs: number) {
    stopPolling();
    update((s) => ({
      ...s,
      polling: { active: true, intervalMs }
    }));
    pollingTimer = setInterval(async () => {
      try {
        // GET /options/session/{underlying}
        const snap = await getOptionsSession(underlying);
        update((s) => ({
          ...s,
          latestSnapshot: snap
        }));
      } catch (err: any) {
        if ((err as any)?.status === 404) {
          // No active session — set empty state and stop polling
          update((s) => ({
            ...s,
            latestSnapshot: null,
            connectionStatus: 'idle'
          }));
          stopPolling();
        } else {
          // keep polling; record last error
          update((s) => ({
            ...s,
            errors: { last: err?.message ?? 'Polling error' }
          }));
        }
      }
    }, Math.max(1000, intervalMs)) as unknown as number;
  }

  function setStatus(status: ConnectionStatus) {
    update((s) => ({ ...s, connectionStatus: status }));
  }

  // ----- Core actions -----

  /**
   * GET /options/session/{underlying}
   * Fetch a one-shot snapshot; special-case 404 to set empty state.
   */
  async function fetchSnapshot(underlying: string): Promise<OptionsSessionSnapshot | null> {
    try {
      const snap = await getOptionsSession(underlying);
      update((s) => {
        const nextCadence = snap?.cadence_sec ?? s.config.cadence_sec;
        return {
          ...s,
          latestSnapshot: snap,
          // refresh polling interval suggestion based on server cadence
          polling: {
            ...s.polling,
            intervalMs: Math.max(1000, (nextCadence || 5) * 1000)
          }
        };
      });
      return snap;
    } catch (err: any) {
      if ((err as any)?.status === 404) {
        update((s) => ({
          ...s,
          latestSnapshot: null,
          connectionStatus: 'idle'
        }));
        return null;
      }
      update((s) => ({
        ...s,
        errors: { last: err?.message ?? 'Failed to fetch snapshot' }
      }));
      return null;
    }
  }

  /**
   * Establish WS /ws/options/session/{underlying}
   * - Updates connectionStatus lifecycle
   * - onMessage merges latestSnapshot
   */
  function connectWs(underlying: string) {
    if (!browser) return;
    // Avoid duplicate connection for same underlying
    if (ws && ws.readyState === WebSocket.OPEN && wsUnderlying === underlying) {
      return;
    }

    // Always cleanup first to prevent multiple sockets
    disconnectWs();

    setStatus('connecting');
    manualDisconnect = false;
    wsUnderlying = underlying;

    ws = connectOptionsSessionWS(
      underlying,
      // onMessage
      (snapshot) => {
        update((s) => ({
          ...s,
          latestSnapshot: snapshot
        }));
      },
      // onOpen
      () => {
        setStatus('connected');
        reconnectAttempts = 0;
        clearReconnectTimer();
        stopPolling(); // stop fallback polling on successful WS connect
      },
      // onClose
      (ev) => handleWsClose(ev),
      // onError
      (ev) => {
        update((s) => ({
          ...s,
          errors: { last: 'WebSocket error' }
        }));
        setStatus('error');
      }
    );
  }

  /**
   * Disconnect current WS if present; set status appropriately and stop polling/reconnect timers.
   */
  function disconnectWs() {
    manualDisconnect = true;
    clearReconnectTimer();
    if (ws) {
      try {
        ws.close();
      } catch {
        // ignore
      }
    }
    ws = null;
    wsUnderlying = null;
    stopPolling();
    // Return to idle if explicitly disconnected
    setStatus('idle');
  }

  /**
   * Handle WS close:
   * - code===4004 => empty state (no session), do not reconnect
   * - else => schedule reconnect with exponential backoff and start fallback polling
   */
  function handleWsClose(ev: CloseEvent) {
    ws = null;
    // If this was a manual disconnect (switching underlying / stopping), do not attempt reconnect
    if (manualDisconnect) {
      manualDisconnect = false;
      return;
    }

    if (ev.code === 4004) {
      // Session not found on server
      update((s) => ({
        ...s,
        latestSnapshot: null,
        connectionStatus: 'idle'
      }));
      clearReconnectTimer();
      stopPolling(); // no session => keep idle, no polling
      return;
    }

    // Schedule reconnect with backoff
    setStatus('error');
    reconnectAttempts += 1;
    const delay = computeBackoffMs(reconnectAttempts);
    clearReconnectTimer();
    const underlying = wsUnderlying ?? null;

    // Start fallback polling while we wait to reconnect (if we still have an underlying)
    if (underlying) {
      let intervalMs = 5000;
      // Prefer cadence from last snapshot or config
      let cadenceSec: number | undefined;
      let selectedCadence = 5;
      // snapshot cadence derived inside update state read
      update((s) => {
        cadenceSec = s.latestSnapshot?.cadence_sec ?? s.config.cadence_sec ?? 5;
        selectedCadence = cadenceSec || 5;
        intervalMs = Math.max(1000, selectedCadence * 1000);
        return s;
      });
      startPolling(underlying, intervalMs);
    }

    reconnectTimer = setTimeout(() => {
      if (underlying) {
        setStatus('connecting');
        connectWs(underlying);
      }
    }, delay) as unknown as number;
  }

  /**
   * Set selected underlying:
   * - sets state
   * - fetches snapshot via GET /options/session/{underlying}
   * - connects WS if snapshot exists (not 404)
   */
  async function selectUnderlying(underlying: string) {
    update((s) => ({
      ...s,
      selectedUnderlying: underlying,
      selectedExpiry: null,
      errors: null
    }));

    // Make sure no stale connection remains
    disconnectWs();

    const snap = await fetchSnapshot(underlying);
    if (snap) {
      connectWs(underlying);
    } else {
      // 404 empty-state: leave WS disconnected
      setStatus('idle');
    }
  }

  /**
   * POST /options/sessions
   * Start/update/replace sessions, update watchlist. Auto-select first item if none is selected.
   */
  async function startSessions(items: SessionRequestItem[], replace: boolean) {
    try {
      const watch = await postOptionsSessions({
        items,
        replace
      });
      update((s) => ({
        ...s,
        watchlist: watch,
        config: { ...s.config, replace }
      }));

      // Auto-select first requested underlying if none selected
      let toSelect: string | null = null;
      update((s) => {
        if (!s.selectedUnderlying && items.length > 0) {
          toSelect = items[0].underlying;
        }
        return s;
      });

      const target = toSelect || items[0]?.underlying;
      if (target) {
        await selectUnderlying(target);
      } else {
        // If selection unchanged but session may have started for current underlying, ensure connect
        let cur: string | null = null;
        update((s) => (cur = s.selectedUnderlying, s));
        if (cur) {
          const snap = await fetchSnapshot(cur);
          if (snap) connectWs(cur);
        }
      }
    } catch (err: any) {
      update((s) => ({
        ...s,
        errors: { last: err?.message ?? 'Failed to start sessions' }
      }));
    }
  }

  /**
   * DELETE /options/session/{underlying}
   * Stop one session. If it's the selected underlying, disconnect WS and clear snapshot.
   */
  async function stopSession(underlying: string) {
    try {
      const resp: StopSessionResponse = await deleteOptionsSession(underlying);
      // Update watchlist: mark as not running or remove
      update((s) => ({
        ...s,
        watchlist: s.watchlist.filter((w) => w.underlying !== underlying)
      }));

      // If stopping currently selected underlying
      let selected: string | null = null;
      update((s) => (selected = s.selectedUnderlying, s));
      if (selected === underlying) {
        disconnectWs();
        update((s) => ({
          ...s,
          latestSnapshot: null,
          connectionStatus: 'idle'
        }));
      }
      return resp;
    } catch (err: any) {
      update((s) => ({
        ...s,
        errors: { last: err?.message ?? 'Failed to stop session' }
      }));
      throw err;
    }
  }

  /**
   * Fallback polling if WS fails: GET snapshot every N seconds until WS reconnects.
   */
  function fallbackPollIfWsFails() {
    let u: string | null = null;
    let intervalMs = 5000;
    update((s) => {
      u = s.selectedUnderlying;
      const cadence = s.latestSnapshot?.cadence_sec ?? s.config.cadence_sec ?? 5;
      intervalMs = Math.max(1000, cadence * 1000);
      return s;
    });
    if (u) startPolling(u, intervalMs);
  }

  /**
   * Update config fields (window, cadence_sec, replace). Does not side-effect sessions; caller should POST if needed.
   */
  function setConfig(partial: Partial<OptionsStoreState['config']>) {
    update((s) => ({
      ...s,
      config: { ...s.config, ...partial }
    }));
  }

  /**
   * Select only the expiry (does not fetch or reconnect).
   */
  function selectExpiry(expiry: string | null) {
    update((s) => ({
      ...s,
      selectedExpiry: expiry
    }));
  }

  return {
    subscribe,

    // State mutators
    selectUnderlying,
    selectExpiry,
    setConfig,

    // Session management
    startSessions,
    fetchSnapshot,
    stopSession,

    // WS lifecycle
    connectWs,
    disconnectWs,
    handleWsClose,
    fallbackPollIfWsFails
  };
}

export const optionsStore = createOptionsStore();