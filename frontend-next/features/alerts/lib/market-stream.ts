/**
 * One websocket for the whole alerts area.
 *
 * Nothing else in the alerts UI talks to a socket: components declare which
 * canonical instruments they currently need, this client reference-counts those
 * registrations, keeps one connection, and pushes per-instrument updates to the
 * listeners that asked for that instrument only. That last part is what keeps a
 * tick from rerendering every row on the page.
 *
 * The client is deliberately framework-free (the React binding lives in
 * `use-market-stream.tsx`) so its lifecycle — dedupe, diffs, reconnect replay,
 * bounded storage — is testable without rendering anything.
 *
 * It never holds a credential: the server authenticates the socket with the
 * application session cookie and decides what the connection may see.
 */

export type QuoteFreshness = "LIVE" | "DELAYED" | "STALE" | "MARKET CLOSED" | "NO DATA";
export type SessionState = "open" | "closed" | "unknown";
export type StreamConnectionState = "idle" | "connecting" | "live" | "reconnecting" | "closed";

export type MarketQuote = {
  instrument_key: string;
  broker_token: number | null;
  last_price: number | null;
  change_absolute: number | null;
  change_percent: number | null;
  ohlc: Record<string, number> | null;
  exchange_timestamp: string | null;
  received_at: string | null;
  server_time: string | null;
  age_ms: number | null;
  session_state: SessionState;
  freshness: QuoteFreshness;
  origin: "snapshot" | "tick";
};

export type StreamStatus = {
  state: StreamConnectionState;
  /** Server-side view of the runtime, when the server has told us. */
  runtime: Record<string, unknown> | null;
  /** Last error code the server sent (e.g. INSTRUMENT_LIMIT), or null. */
  error: { code: string; message: string; instrument?: string } | null;
  /** Frame types received, for tests and diagnostics. */
  frames: number;
  reconnects: number;
};

export type SocketLike = {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
};

export type StreamOptions = {
  /** Where the socket lives; the provider supplies the scope-aware path. */
  url: () => string;
  socketFactory?: (url: string) => SocketLike;
  now?: () => number;
  /** Diff debounce for subscribe/unsubscribe bursts. */
  diffDebounceMs?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  /** Cap on remembered quotes (registered keys are never evicted first). */
  maxQuotes?: number;
  openTimeoutMs?: number;
};

type QuoteListener = (quote: MarketQuote | undefined) => void;
type StatusListener = (status: StreamStatus) => void;

const HEARTBEAT_GRACE_MS = 45_000;

export class AlertsMarketStream {
  private readonly options: Required<Pick<StreamOptions, "diffDebounceMs" | "baseBackoffMs" | "maxBackoffMs" | "maxQuotes" | "openTimeoutMs">> &
    StreamOptions;
  private socket: SocketLike | null = null;
  private url: string | null = null;
  private refcounts = new Map<string, number>();
  private quotes = new Map<string, MarketQuote>();
  private quoteListeners = new Map<string, Set<QuoteListener>>();
  private statusListeners = new Set<StatusListener>();
  private status: StreamStatus = {
    state: "idle",
    runtime: null,
    error: null,
    frames: 0,
    reconnects: 0,
  };
  private diffTimer: ReturnType<typeof setTimeout> | null = null;
  private backoffTimer: ReturnType<typeof setTimeout> | null = null;
  private openTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;
  private lastMessageAt: number | null = null;
  private destroyed = false;

  constructor(options: StreamOptions) {
    this.options = {
      diffDebounceMs: 50,
      baseBackoffMs: 500,
      maxBackoffMs: 8_000,
      maxQuotes: 200,
      openTimeoutMs: 10_000,
      ...options,
    };
  }

  // -- registration ----------------------------------------------------

  /** Declare that a component currently needs this instrument. */
  register(key: string | null | undefined): () => void {
    const instrument = (key ?? "").trim();
    if (!instrument || this.destroyed) {
      return () => undefined;
    }
    this.refcounts.set(instrument, (this.refcounts.get(instrument) ?? 0) + 1);
    this.scheduleDiff();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const count = (this.refcounts.get(instrument) ?? 0) - 1;
      if (count <= 0) {
        this.refcounts.delete(instrument);
      } else {
        this.refcounts.set(instrument, count);
      }
      this.scheduleDiff();
    };
  }

  registeredKeys(): string[] {
    return [...this.refcounts.keys()].sort();
  }

  // -- reads -----------------------------------------------------------

  getQuote(key: string | null | undefined): MarketQuote | undefined {
    const instrument = (key ?? "").trim();
    return instrument ? this.quotes.get(instrument) : undefined;
  }

  getStatus(): StreamStatus {
    return { ...this.status };
  }

  /** Whether the stream is usable for display purposes. */
  isLive(): boolean {
    return this.status.state === "live";
  }

  subscribeQuote(key: string | null | undefined, listener: QuoteListener): () => void {
    const instrument = (key ?? "").trim();
    if (!instrument) {
      return () => undefined;
    }
    const set = this.quoteListeners.get(instrument) ?? new Set<QuoteListener>();
    set.add(listener);
    this.quoteListeners.set(instrument, set);
    return () => {
      const current = this.quoteListeners.get(instrument);
      if (!current) return;
      current.delete(listener);
      if (current.size === 0) this.quoteListeners.delete(instrument);
    };
  }

  subscribeStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  // -- connection ------------------------------------------------------

  private socketFactory(url: string): SocketLike {
    if (this.options.socketFactory) {
      return this.options.socketFactory(url);
    }
    return new WebSocket(url) as unknown as SocketLike;
  }

  private scheduleDiff(): void {
    if (this.destroyed) return;
    if (this.diffTimer) return;
    this.diffTimer = setTimeout(() => {
      this.diffTimer = null;
      this.connectIfNeeded();
      this.pushDiff();
    }, this.options.diffDebounceMs);
  }

  private connectIfNeeded(): void {
    if (this.destroyed) return;
    if (this.refcounts.size === 0) {
      return; // no consumers: do not hold a socket open
    }
    const target = this.options.url();
    if (this.socket && this.url === target) {
      return;
    }
    if (this.socket) {
      // Scope/URL changed under us: replace the connection and replay.
      this.teardownSocket();
    }
    this.url = target;
    this.open();
  }

  private open(): void {
    const url = this.url;
    if (!url) return;
    this.setStatus({ state: this.attempts === 0 ? "connecting" : "reconnecting" });
    let socket: SocketLike;
    try {
      socket = this.socketFactory(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    this.openTimer = setTimeout(() => {
      // A socket that never opens must not leave the UI waiting forever.
      if (this.socket === socket && this.status.state !== "live") {
        this.teardownSocket();
        this.scheduleReconnect();
      }
    }, this.options.openTimeoutMs);

    socket.onopen = () => {
      if (this.openTimer) {
        clearTimeout(this.openTimer);
        this.openTimer = null;
      }
      this.attempts = 0;
      this.lastMessageAt = this.options.now?.() ?? Date.now();
      this.setStatus({ state: "live", error: null });
      // Replay the full desired set: a reconnect must not lose subscriptions,
      // and the client's own view must match what it just asked for.
      const desired = this.registeredKeys();
      this.requested = new Set(desired);
      this.send({ type: "subscribe", instruments: desired });
    };
    socket.onmessage = (event) => this.handleFrame(event?.data);
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.teardownSocket();
      if (this.refcounts.size > 0) {
        this.scheduleReconnect();
      } else {
        this.setStatus({ state: "closed" });
      }
    };
    socket.onerror = () => {
      // The close handler owns reconnection; error alone is not terminal.
    };
  }

  private scheduleReconnect(): void {
    if (this.destroyed || this.backoffTimer) return;
    const attempt = this.attempts++;
    const base = Math.min(this.options.baseBackoffMs * 2 ** attempt, this.options.maxBackoffMs);
    // Jitter avoids a fleet of tabs reconnecting in lockstep.
    const delay = Math.round(base / 2 + Math.random() * (base / 2));
    this.setStatus({ state: "reconnecting", reconnects: this.status.reconnects + 1 });
    this.backoffTimer = setTimeout(() => {
      this.backoffTimer = null;
      this.connectIfNeeded();
    }, delay);
  }

  private teardownSocket(): void {
    if (this.openTimer) {
      clearTimeout(this.openTimer);
      this.openTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      try {
        socket.close();
      } catch {
        // already closed
      }
    }
  }

  private pushDiff(): void {
    if (!this.socket || this.status.state !== "live") return;
    const desired = this.registeredKeys();
    const toAdd = desired.filter((key) => !this.requested.has(key));
    const toRemove = [...this.requested].filter((key) => !this.refcounts.has(key));
    if (toAdd.length > 0) {
      this.send({ type: "subscribe", instruments: toAdd });
      for (const key of toAdd) this.requested.add(key);
    }
    if (toRemove.length > 0) {
      this.send({ type: "unsubscribe", instruments: toRemove });
      for (const key of toRemove) this.requested.delete(key);
    }
  }

  /**
   * What this client has asked the server to stream.
   *
   * Diffs are computed against THIS, not against the server's `subscriptions`
   * ack: an ack can be missed, arrive late, or be partially refused at the cap,
   * and a client that trusted it would then never unsubscribe (a leak) or
   * re-subscribe (a stale row). The ack is kept only for the recorded server
   * view and the cap error the UI shows.
   */
  private requested = new Set<string>();
  private serverSubscriptions: string[] = [];

  private send(payload: Record<string, unknown>): boolean {
    if (!this.socket || this.status.state !== "live") return false;
    try {
      this.socket.send(JSON.stringify(payload));
      return true;
    } catch {
      // A send that throws means the socket died; onclose will recover it and
      // the replay will re-ask for everything.
      return false;
    }
  }

  // -- frames ----------------------------------------------------------

  /** Exposed for tests; the socket handler is the only production caller. */
  handleFrame(raw: unknown): void {
    if (typeof raw !== "string") return;
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return;
    }
    this.lastMessageAt = this.options.now?.() ?? Date.now();
    this.status.frames += 1;
    const type = String(frame.type ?? "");
    if (type === "welcome") {
      this.setStatus({ runtime: (frame.runtime as Record<string, unknown>) ?? null });
      return;
    }
    if (type === "quote") {
      this.applyQuote(frame as unknown as MarketQuote);
      return;
    }
    if (type === "state") {
      this.applyState(frame);
      return;
    }
    if (type === "subscriptions") {
      this.serverSubscriptions = Array.isArray(frame.instruments)
        ? (frame.instruments as string[])
        : [];
      this.setStatus({ error: null });
      return;
    }
    if (type === "heartbeat") {
      this.setStatus({ runtime: (frame.runtime as Record<string, unknown>) ?? this.status.runtime });
      return;
    }
    if (type === "error") {
      this.setStatus({
        error: {
          code: String(frame.code ?? "UNKNOWN"),
          message: String(frame.message ?? "the stream refused the request"),
          instrument: typeof frame.instrument === "string" ? frame.instrument : undefined,
        },
      });
    }
  }

  private applyQuote(frame: MarketQuote): void {
    const key = String(frame.instrument_key ?? "");
    if (!key) return;
    const quote: MarketQuote = {
      ...frame,
      freshness: (frame.freshness ?? "NO DATA") as QuoteFreshness,
      session_state: (frame.session_state ?? "unknown") as SessionState,
    };
    this.quotes.set(key, quote);
    this.evictIfNeeded();
    this.notifyQuote(key);
  }

  private applyState(frame: Record<string, unknown>): void {
    const instruments = frame.instruments;
    if (instruments && typeof instruments === "object") {
      for (const [key, value] of Object.entries(instruments as Record<string, unknown>)) {
        if (!value || typeof value !== "object") continue;
        const next = value as Record<string, unknown>;
        const existing = this.quotes.get(key);
        if (existing) {
          this.quotes.set(key, {
            ...existing,
            freshness: (next.freshness as QuoteFreshness) ?? existing.freshness,
            session_state: (next.session_state as SessionState) ?? existing.session_state,
            age_ms: typeof next.age_ms === "number" ? next.age_ms : existing.age_ms,
          });
        } else {
          this.quotes.set(key, {
            instrument_key: key,
            broker_token: null,
            last_price: null,
            change_absolute: null,
            change_percent: null,
            ohlc: null,
            exchange_timestamp: null,
            received_at: null,
            server_time: null,
            age_ms: typeof next.age_ms === "number" ? next.age_ms : null,
            session_state: (next.session_state as SessionState) ?? "unknown",
            freshness: (next.freshness as QuoteFreshness) ?? "NO DATA",
            origin: "snapshot",
          });
        }
        this.notifyQuote(key);
      }
    }
    if (frame.runtime && typeof frame.runtime === "object") {
      this.setStatus({ runtime: frame.runtime as Record<string, unknown> });
    }
  }

  private evictIfNeeded(): void {
    if (this.quotes.size <= this.options.maxQuotes) return;
    const removable = [...this.quotes.keys()].filter((key) => !this.refcounts.has(key));
    for (const key of removable) {
      if (this.quotes.size <= this.options.maxQuotes) break;
      this.quotes.delete(key);
    }
  }

  private notifyQuote(key: string): void {
    const listeners = this.quoteListeners.get(key);
    if (!listeners || listeners.size === 0) return;
    const quote = this.quotes.get(key);
    for (const listener of listeners) {
      listener(quote);
    }
  }

  private setStatus(patch: Partial<StreamStatus>): void {
    const next: StreamStatus = { ...this.status, ...patch, state: patch.state ?? this.status.state };
    const changed =
      next.state !== this.status.state ||
      next.error !== this.status.error ||
      next.reconnects !== this.status.reconnects ||
      next.runtime !== this.status.runtime;
    this.status = next;
    if (!changed) return;
    for (const listener of this.statusListeners) {
      listener(this.getStatus());
    }
  }

  /** Milliseconds since the last frame, or null when nothing has arrived. */
  silenceMs(): number | null {
    if (this.lastMessageAt === null) return null;
    return (this.options.now?.() ?? Date.now()) - this.lastMessageAt;
  }

  /** True when the socket is open but silent for longer than the grace. */
  looksSilent(): boolean {
    const silence = this.silenceMs();
    return silence !== null && silence > HEARTBEAT_GRACE_MS;
  }

  destroy(): void {
    this.destroyed = true;
    if (this.diffTimer) clearTimeout(this.diffTimer);
    if (this.backoffTimer) clearTimeout(this.backoffTimer);
    this.diffTimer = null;
    this.backoffTimer = null;
    this.teardownSocket();
    this.refcounts.clear();
    this.quotes.clear();
    this.quoteListeners.clear();
    this.statusListeners.clear();
  }
}

/** Build the alerts stream URL for the current page origin. */
export function alertsMarketStreamUrl(scope: string | null, path = "/api/alerts/market/ws"): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const base = origin && origin !== "null" ? origin : "http://localhost";
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (scope) url.searchParams.set("scope", scope);
  return url.toString();
}
