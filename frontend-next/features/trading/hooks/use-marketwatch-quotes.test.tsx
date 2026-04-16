import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useMarketwatchQuotes } from "@/features/trading/hooks/use-marketwatch-quotes";

class MockSocket {
  static instances: MockSocket[] = [];

  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();

  constructor() {
    MockSocket.instances.push(this);
  }
}

describe("useMarketwatchQuotes", () => {
  afterEach(() => {
    MockSocket.instances = [];
    vi.unstubAllGlobals();
  });

  it("subscribes to NIFTY and BANKNIFTY and maps ticks into quotes", async () => {
    vi.stubGlobal("WebSocket", MockSocket as unknown as typeof WebSocket);

    const { result } = renderHook(() => useMarketwatchQuotes());

    MockSocket.instances[0].onopen?.();
    MockSocket.instances[0].onmessage?.(
      new MessageEvent("message", {
        data: JSON.stringify({
          type: "ticks",
          data: [
            { instrument_token: 256265, last_price: 24310.25, change: 0.54 },
            { instrument_token: 260105, last_price: 51982.1, change: -0.12 },
          ],
        }),
      }),
    );

    await waitFor(() => expect(result.current.quotes[0].lastPrice).toBe(24310.25));
    expect(result.current.connected).toBe(true);
    expect(result.current.quotes[1].changePercent).toBe(-0.12);
  });
});
