"use client";

import { useEffect, useState } from "react";

type MarketwatchTick = {
  instrument_token: number;
  last_price?: number;
  change?: number;
};

const MARKET_HEADER_ITEMS = [
  { label: "NIFTY", token: 256265 },
  { label: "BANKNIFTY", token: 260105 },
] as const;

function buildMarketwatchWsUrl() {
  if (typeof window === "undefined") {
    return "ws://localhost:3000/ws/marketwatch";
  }
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws/marketwatch`;
}

function formatIstNow() {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

export function TopBar({ title }: Readonly<{ title: string }>) {
  const [time, setTime] = useState("--:--:--");
  const [ticks, setTicks] = useState<Record<number, MarketwatchTick>>({});
  const [marketConnected, setMarketConnected] = useState(false);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setTime(formatIstNow()));
    const interval = window.setInterval(() => setTime(formatIstNow()), 1000);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | null = null;
    let socket: WebSocket | null = null;
    const ownerId = `frontend:topbar:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;

    const connect = () => {
      socket = new WebSocket(buildMarketwatchWsUrl());

      socket.onopen = () => {
        if (disposed || !socket) {
          return;
        }
        setMarketConnected(true);
        socket.send(
          JSON.stringify({
            action: "set_subscriptions",
            owner_id: ownerId,
            tokens: Object.fromEntries(MARKET_HEADER_ITEMS.map((item) => [String(item.token), "quote"])),
          }),
        );
      };

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as {
            type?: string;
            data?: MarketwatchTick[];
            ticks?: MarketwatchTick[] | Record<string, MarketwatchTick>;
          };
          const incomingTicks = Array.isArray(payload.data)
            ? payload.data
            : Array.isArray(payload.ticks)
              ? payload.ticks
              : payload.ticks && typeof payload.ticks === "object"
                ? Object.values(payload.ticks)
                : [];

          if ((payload.type === "ticks" || payload.type === "snapshot") && incomingTicks.length > 0) {
            setTicks((current) => {
              const next = { ...current };
              for (const tick of incomingTicks) {
                if (typeof tick.instrument_token === "number") {
                  next[tick.instrument_token] = tick;
                }
              }
              return next;
            });
          }
        } catch {
          // ignore malformed messages
        }
      };

      socket.onclose = () => {
        setMarketConnected(false);
        if (!disposed) {
          reconnectTimer = window.setTimeout(connect, 2000);
        }
      };

      socket.onerror = () => {
        setMarketConnected(false);
      };
    };

    connect();

    return () => {
      disposed = true;
      setMarketConnected(false);
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: "clear_subscriptions", owner_id: ownerId }));
      }
      socket?.close();
    };
  }, []);

  return (
    <header className="flex h-10 items-center gap-2 border-b border-[var(--border)] bg-[var(--panel)] px-4">
      <span className="text-[12px] font-bold tracking-[0.03em] text-[var(--text)]">{title.toUpperCase()}</span>
      <span className="mx-1 h-[18px] w-px bg-[var(--border)]" />
      <input
        readOnly
        aria-label="command palette"
        placeholder="⌘K  jump to anything..."
        className="h-7 w-[240px] rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 text-[11px] text-[var(--dim)] outline-none"
      />
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden" aria-live="polite">
        {MARKET_HEADER_ITEMS.map((item) => {
          const tick = ticks[item.token];
          const change = typeof tick?.change === "number" ? tick.change : null;
          return (
            <span key={item.token} className="inline-flex min-w-0 items-center gap-1 rounded-[4px] border border-[var(--border)] px-2 py-0.5 text-[10px] font-semibold">
              <span className={`h-[5px] w-[5px] rounded-full ${marketConnected ? "bg-[var(--green)]" : "bg-[var(--muted)]"}`} />
              <span className="uppercase tracking-[0.12em] text-[var(--dim)]">{item.label}</span>
              <span className="font-mono text-[var(--text)]">{typeof tick?.last_price === "number" ? tick.last_price.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}</span>
              <span className={`font-mono ${change === null ? "text-[var(--dim)]" : change >= 0 ? "text-[var(--green)]" : "text-[var(--red)]"}`}>
                {change === null ? "—" : `${change >= 0 ? "▲" : "▼"}${Math.abs(change).toFixed(2)}%`}
              </span>
            </span>
          );
        })}
      </div>
      <span className="rounded-[4px] border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)]">paper · IST</span>
      <span className="font-mono text-[11px] text-[var(--dim)]">{time}</span>
    </header>
  );
}
