"use client";

/**
 * React binding for the alerts live-price stream.
 *
 * One provider for the whole alerts area; components ask for the instruments
 * they currently show. The two hooks that matter in practice:
 *
 * * `useMarketQuote(key)` — registers the instrument (refcounted, so duplicate
 *   rows share one subscription) and re-renders only when THAT instrument's
 *   quote or the connection state changes;
 * * `useInViewport()` — the list uses it so a row only holds a subscription
 *   while it is actually visible.
 *
 * `useQuotePresentation` is the single place that decides what label a price
 * carries, which is why the "never show a cached price as LIVE" rule lives here:
 * a socket that is reconnecting or silent downgrades the label even when the
 * last quote said LIVE.
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  AlertsMarketStream,
  alertsMarketStreamUrl,
  type MarketQuote,
  type QuoteFreshness,
  type StreamStatus,
} from "@/features/alerts/lib/market-stream";

type Presentation = {
  label: string;
  /** Live data, honest about connection and market state. */
  tone: "positive" | "warning" | "danger" | "neutral";
  price: number | null;
  fresh: boolean;
};

const STREAM_BY_SCOPE = new Map<string, AlertsMarketStream>();

function streamFor(scope: string | null): AlertsMarketStream {
  const key = scope ?? "__default__";
  const existing = STREAM_BY_SCOPE.get(key);
  if (existing) return existing;
  const created = new AlertsMarketStream({ url: () => alertsMarketStreamUrl(scope) });
  STREAM_BY_SCOPE.set(key, created);
  return created;
}

const StreamContext = createContext<AlertsMarketStream | null>(null);

export function AlertsMarketStreamProvider({
  scope,
  children,
}: {
  scope: string | null;
  children: ReactNode;
}) {
  const stream = useMemo(() => streamFor(scope), [scope]);
  return <StreamContext.Provider value={stream}>{children}</StreamContext.Provider>;
}

function useStream(): AlertsMarketStream {
  const stream = useContext(StreamContext);
  if (stream === null) {
    throw new Error("AlertsMarketStreamProvider is missing above this component");
  }
  return stream;
}

/** The live quote for one canonical instrument, or undefined before data. */
export function useMarketQuote(key: string | null | undefined): {
  quote: MarketQuote | undefined;
  status: StreamStatus;
} {
  const stream = useStream();
  const [snapshot, setSnapshot] = useState<MarketQuote | undefined>(() => stream.getQuote(key));
  const [status, setStatus] = useState<StreamStatus>(() => stream.getStatus());

  useEffect(() => {
    // Register first, then adopt whatever the store already has: the effect
    // subscribes to the external system, and the refresh happens in the
    // subscription callback (plus once here for the case where a quote already
    // exists and no frame is coming).
    const unregister = stream.register(key);
    const offQuote = stream.subscribeQuote(key, (next) => setSnapshot(next));
    const offStatus = stream.subscribeStatus(setStatus);
    if (stream.getQuote(key) !== snapshot) {
      setSnapshot(stream.getQuote(key));
    }
    return () => {
      offQuote();
      offStatus();
      unregister();
    };
    // `snapshot` is intentionally read but not a dependency: it would resubscribe
    // on every frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream, key]);

  return { quote: stream.getQuote(key) ?? snapshot, status };
}

/** Live quotes for a bounded set of instruments (detail page member list). */
export function useMarketQuotes(keys: string[]): Record<string, MarketQuote | undefined> {
  const stream = useStream();
  const joined = keys.join(",");
  const [quotes, setQuotes] = useState<Record<string, MarketQuote | undefined>>({});

  useEffect(() => {
    const list = joined ? joined.split(",") : [];
    const refresh = () => {
      const next: Record<string, MarketQuote | undefined> = {};
      for (const key of list) next[key] = stream.getQuote(key);
      setQuotes(next);
    };
    const unregister = list.map((key) => stream.register(key));
    const off = list.map((key) => stream.subscribeQuote(key, refresh));
    const offStatus = stream.subscribeStatus(refresh);
    return () => {
      off.forEach((fn) => fn());
      offStatus();
      unregister.forEach((fn) => fn());
    };
  }, [stream, joined]);

  return quotes;
}

export function useMarketStreamStatus(): StreamStatus {
  const stream = useStream();
  const [status, setStatus] = useState<StreamStatus>(() => stream.getStatus());
  useEffect(() => stream.subscribeStatus(setStatus), [stream]);
  return status;
}

/**
 * Register an instrument only while its element is visible.
 *
 * The list uses this so scrolling away releases the subscription; without it a
 * long list would hold every row's instrument open until the page closed.
 */
export function useInViewport<T extends Element>(
  options: { rootMargin?: string; enabled?: boolean } = {},
): { ref: React.RefObject<T | null>; inViewport: boolean } {
  const { rootMargin = "120px", enabled = true } = options;
  const ref = useRef<T | null>(null);
  const [observed, setObserved] = useState(false);
  // No IntersectionObserver (older browser, jsdom) means "assume visible": the
  // row keeps its subscription instead of silently losing live data.
  const supported =
    typeof IntersectionObserver !== "undefined" || typeof window === "undefined";

  useEffect(() => {
    if (!enabled) return;
    const element = ref.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          setObserved(entry.isIntersecting);
        }
      },
      { rootMargin },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled, rootMargin]);

  const inViewport = !enabled ? false : supported ? observed : true;
  return { ref, inViewport };
}

/** The label and tone for a quote, honest about connection and market state. */
export function useQuotePresentation(
  quote: MarketQuote | undefined,
  status: StreamStatus,
): Presentation {
  return useMemo(() => presentQuote(quote, status), [quote, status]);
}

export function presentQuote(
  quote: MarketQuote | undefined,
  status: StreamStatus,
): Presentation {
  const price = quote?.last_price ?? null;
  if (status.state === "reconnecting" || status.state === "connecting") {
    // We cannot know the price is still live while the socket is down, whatever
    // the last frame said.
    return { label: "RECONNECTING", tone: "warning", price, fresh: false };
  }
  if (status.state === "closed" || status.state === "idle") {
    return { label: quote ? "STALE" : "NO DATA", tone: "neutral", price, fresh: false };
  }
  const label = (quote?.freshness ?? "NO DATA") as QuoteFreshness;
  const toneByLabel: Record<QuoteFreshness, Presentation["tone"]> = {
    LIVE: "positive",
    DELAYED: "warning",
    STALE: "danger",
    "MARKET CLOSED": "neutral",
    "NO DATA": "neutral",
  };
  return { label, tone: toneByLabel[label], price, fresh: label === "LIVE" };
}
