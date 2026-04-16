"use client";

import { useEffect, useState } from "react";

import type { MarketQuote } from "@/features/trading/types";

type MarketwatchTick = {
  instrument_token: number;
  last_price?: number;
  change?: number;
};

const TOKENS = {
  NIFTY: 256265,
  BANKNIFTY: 260105,
} as const;

const DEFAULT_QUOTES: MarketQuote[] = [
  { symbol: "NIFTY", token: TOKENS.NIFTY, lastPrice: null, changePercent: null, connected: false },
  { symbol: "BANKNIFTY", token: TOKENS.BANKNIFTY, lastPrice: null, changePercent: null, connected: false },
];

type StoreState = {
  connected: boolean;
  ticks: Record<number, MarketwatchTick>;
};

const listeners = new Set<(state: StoreState) => void>();
let storeState: StoreState = { connected: false, ticks: {} };
let socket: WebSocket | null = null;
let reconnectTimer: number | null = null;
let ownerId: string | null = null;

function buildUrl() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws/marketwatch`;
}

function emit() {
  for (const listener of listeners) {
    listener(storeState);
  }
}

function connect() {
  if (typeof window === "undefined" || socket) {
    return;
  }

  ownerId = ownerId ?? `frontend:trading:${window.crypto?.randomUUID?.() ?? "marketwatch"}`;
  socket = new WebSocket(buildUrl());

  socket.onopen = () => {
    storeState = { ...storeState, connected: true };
    emit();
    socket?.send(
      JSON.stringify({
        action: "set_subscriptions",
        owner_id: ownerId,
        tokens: {
          [String(TOKENS.NIFTY)]: "quote",
          [String(TOKENS.BANKNIFTY)]: "quote",
        },
      }),
    );
  };

  socket.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data) as { data?: MarketwatchTick[]; ticks?: MarketwatchTick[] };
      const incoming = Array.isArray(payload.data) ? payload.data : Array.isArray(payload.ticks) ? payload.ticks : [];
      if (incoming.length === 0) {
        return;
      }
      const ticks = { ...storeState.ticks };
      for (const tick of incoming) {
        ticks[tick.instrument_token] = tick;
      }
      storeState = { ...storeState, ticks };
      emit();
    } catch {
      // ignore malformed payloads
    }
  };

  socket.onerror = () => {
    storeState = { ...storeState, connected: false };
    emit();
  };

  socket.onclose = () => {
    socket = null;
    storeState = { ...storeState, connected: false };
    emit();
    if (listeners.size > 0) {
      reconnectTimer = window.setTimeout(connect, 2_000);
    }
  };
}

function disconnectIfIdle() {
  if (listeners.size > 0) {
    return;
  }
  if (reconnectTimer) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket?.readyState === WebSocket.OPEN && ownerId) {
    socket.send(JSON.stringify({ action: "clear_subscriptions", owner_id: ownerId }));
  }
  socket?.close();
  socket = null;
  storeState = { connected: false, ticks: {} };
}

function toQuotes(state: StoreState): MarketQuote[] {
  return DEFAULT_QUOTES.map((quote) => ({
    ...quote,
    connected: state.connected,
    lastPrice: state.ticks[quote.token]?.last_price ?? null,
    changePercent: state.ticks[quote.token]?.change ?? null,
  }));
}

export function useMarketwatchQuotes() {
  const [state, setState] = useState<StoreState>(storeState);

  useEffect(() => {
    listeners.add(setState);
    connect();
    return () => {
      listeners.delete(setState);
      disconnectIfIdle();
    };
  }, []);

  return {
    connected: state.connected,
    quotes: toQuotes(state),
  };
}
