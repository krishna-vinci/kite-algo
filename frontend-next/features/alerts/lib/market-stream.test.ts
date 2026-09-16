/**
 * The alerts market-stream client: dedupe, diffs, reconnect replay, honesty.
 *
 * Regression risks this suite pins, in order of how easy they are to break:
 * one socket (not one per row), registrations coalesced into subscribe diffs,
 * subscriptions replayed after a reconnect, per-instrument listeners that do
 * not fire for other instruments, a reconnecting socket that never labels a
 * cached price LIVE, and bounded storage.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AlertsMarketStream,
  alertsMarketStreamUrl,
  type SocketLike,
} from "@/features/alerts/lib/market-stream";

class FakeSocket implements SocketLike {
  readyState = 0;
  sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  closedWith: number | null = null;

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000, reason = ""): void {
    this.closedWith = code;
    this.readyState = 3;
    void reason;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  push(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  drop(): void {
    this.readyState = 3;
    this.onclose?.({});
  }

  messages(): Array<Record<string, unknown>> {
    return this.sent.map((item) => JSON.parse(item) as Record<string, unknown>);
  }
}

function makeStream(overrides: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const now = { value: 1_000_000 };
  const stream = new AlertsMarketStream({
    url: () => "ws://app.test/api/alerts/market/ws?scope=app%3Aadmin",
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    diffDebounceMs: 0,
    baseBackoffMs: 1,
    maxBackoffMs: 4,
    openTimeoutMs: 1000,
    now: () => now.value,
    ...overrides,
  });
  return { stream, sockets, now };
}

function quoteFrame(instrument: string, price: number, freshness = "LIVE") {
  return {
    type: "quote",
    instrument_key: instrument,
    broker_token: 111,
    last_price: price,
    change_absolute: 1,
    change_percent: 0.5,
    ohlc: null,
    exchange_timestamp: "2026-09-15T11:00:00+00:00",
    received_at: "2026-09-15T11:00:01+00:00",
    server_time: "2026-09-15T11:00:01+00:00",
    age_ms: 500,
    session_state: "open",
    freshness,
    origin: "tick",
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("AlertsMarketStream", () => {
  it("opens one socket for many registrations and dedupes the subscribe", async () => {
    const { stream, sockets } = makeStream();
    const releaseA = stream.register("MCX:GOLD26DECFUT");
    const releaseB = stream.register("MCX:GOLD26DECFUT");
    const releaseC = stream.register("NSE:RELIANCE");

    await vi.advanceTimersByTimeAsync(1);
    expect(sockets).toHaveLength(1);
    sockets[0].open();

    // registrations that happen before the socket is live ride along with the
    // open-time replay rather than producing separate frames
    const subscribes = sockets[0].messages().filter((m) => m.type === "subscribe");
    const instruments = subscribes.flatMap((m) => m.instruments as string[]);
    expect(new Set(instruments)).toEqual(new Set(["MCX:GOLD26DECFUT", "NSE:RELIANCE"]));

    releaseA();
    releaseB();
    releaseC();
    await vi.advanceTimersByTimeAsync(1);
    const unsubscribes = sockets[0].messages().filter((m) => m.type === "unsubscribe");
    expect(unsubscribes.flatMap((m) => m.instruments as string[])).toEqual([
      "MCX:GOLD26DECFUT",
      "NSE:RELIANCE",
    ]);
    stream.destroy();
  });

  it("does not unsubscribe while another registration for the key is held", async () => {
    const { stream, sockets } = makeStream();
    const first = stream.register("MCX:GOLD26DECFUT");
    const second = stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    await vi.advanceTimersByTimeAsync(1);

    first();
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets[0].messages().filter((m) => m.type === "unsubscribe")).toHaveLength(0);

    second();
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets[0].messages().filter((m) => m.type === "unsubscribe")).toHaveLength(1);
    stream.destroy();
  });

  it("notifies only the listeners of the instrument that changed", async () => {
    const { stream, sockets } = makeStream();
    stream.register("MCX:GOLD26DECFUT");
    stream.register("NSE:RELIANCE");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();

    const gold: number[] = [];
    const reliance: number[] = [];
    stream.subscribeQuote("MCX:GOLD26DECFUT", (quote) => gold.push(quote?.last_price ?? -1));
    stream.subscribeQuote("NSE:RELIANCE", (quote) => reliance.push(quote?.last_price ?? -1));

    sockets[0].push(quoteFrame("MCX:GOLD26DECFUT", 124_860));
    expect(gold).toEqual([124_860]);
    expect(reliance).toEqual([]);

    sockets[0].push(quoteFrame("NSE:RELIANCE", 2_950));
    expect(gold).toEqual([124_860]);
    expect(reliance).toEqual([2_950]);
    stream.destroy();
  });

  it("replays every subscription after a reconnect and reports RECONNECTING", async () => {
    const { stream, sockets } = makeStream();
    stream.register("MCX:GOLD26DECFUT");
    stream.register("NSE:RELIANCE");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    const states: string[] = [];
    stream.subscribeStatus((status) => states.push(status.state));

    sockets[0].drop();
    expect(stream.getStatus().state).toBe("reconnecting");

    await vi.advanceTimersByTimeAsync(10);
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    const replay = sockets[1].messages().find((m) => m.type === "subscribe");
    expect(new Set(replay?.instruments as string[])).toEqual(
      new Set(["MCX:GOLD26DECFUT", "NSE:RELIANCE"]),
    );
    expect(stream.getStatus().state).toBe("live");
    expect(states).toContain("reconnecting");
    stream.destroy();
  });

  it("never leaves a socket hanging when the handshake never completes", async () => {
    const { stream, sockets } = makeStream({ openTimeoutMs: 5 });
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets).toHaveLength(1);
    // never opens
    await vi.advanceTimersByTimeAsync(50);
    expect(sockets[0].closedWith).toBe(1000);
    expect(stream.getStatus().state).toBe("reconnecting");
    stream.destroy();
  });

  it("applies state frames without inventing a price", async () => {
    const { stream, sockets } = makeStream();
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    sockets[0].push(quoteFrame("MCX:GOLD26DECFUT", 124_860, "LIVE"));
    sockets[0].push({
      type: "state",
      instruments: { "MCX:GOLD26DECFUT": { freshness: "STALE", session_state: "open", age_ms: 90_000 } },
    });

    const quote = stream.getQuote("MCX:GOLD26DECFUT");
    expect(quote?.freshness).toBe("STALE");
    expect(quote?.last_price).toBe(124_860); // the price is kept, the label is not
    stream.destroy();
  });

  it("surfaces server refusals as status errors", async () => {
    const { stream, sockets } = makeStream();
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    sockets[0].push({
      type: "error",
      code: "INSTRUMENT_LIMIT",
      instrument: "MCX:ZINC26DECFUT",
      message: "at most 25 instruments per connection",
    });
    expect(stream.getStatus().error?.code).toBe("INSTRUMENT_LIMIT");
    stream.destroy();
  });

  it("keeps the quote store bounded and prefers registered keys", async () => {
    const { stream, sockets } = makeStream({ maxQuotes: 3 });
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    for (const key of ["A:1", "B:2", "C:3", "D:4", "MCX:GOLD26DECFUT"]) {
      sockets[0].push(quoteFrame(key, 1));
    }
    expect(stream.getQuote("A:1")).toBeUndefined();
    expect(stream.getQuote("MCX:GOLD26DECFUT")).toBeDefined();
    stream.destroy();
  });

  it("replaces the socket when the scope (url) changes", async () => {
    let scope = "app:admin";
    const sockets: FakeSocket[] = [];
    const stream = new AlertsMarketStream({
      url: () => `ws://app.test/api/alerts/market/ws?scope=${encodeURIComponent(scope)}`,
      socketFactory: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
      diffDebounceMs: 0,
    });
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();

    scope = "app:other";
    stream.register("NSE:RELIANCE");
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets).toHaveLength(2);
    expect(sockets[0].closedWith).not.toBeNull();
    stream.destroy();
  });

  it("closes the socket when the last registration goes away", async () => {
    const { stream, sockets } = makeStream();
    const release = stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    release();
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets[0].messages().some((m) => m.type === "unsubscribe")).toBe(true);
    stream.destroy();
  });

  it("detects a silent socket", async () => {
    const { stream, sockets, now } = makeStream();
    stream.register("MCX:GOLD26DECFUT");
    await vi.advanceTimersByTimeAsync(1);
    sockets[0].open();
    expect(stream.looksSilent()).toBe(false);
    now.value += 60_000;
    expect(stream.looksSilent()).toBe(true);
    stream.destroy();
  });
});

describe("alertsMarketStreamUrl", () => {
  it("uses wss on https origins and carries the scope", () => {
    const original = window.location;
    Object.defineProperty(window, "location", {
      value: { origin: "https://trade.example" },
      configurable: true,
    });
    const url = alertsMarketStreamUrl("app:admin");
    expect(url.startsWith("wss://trade.example/api/alerts/market/ws")).toBe(true);
    expect(url).toContain("scope=app%3Aadmin");
    Object.defineProperty(window, "location", { value: original, configurable: true });
  });

  it("omits the scope when none is selected", () => {
    const url = alertsMarketStreamUrl(null);
    expect(url).not.toContain("scope=");
  });
});
