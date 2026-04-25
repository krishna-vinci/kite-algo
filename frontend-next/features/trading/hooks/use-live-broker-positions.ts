"use client";

import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchBrokerPositions, normalizeBrokerPositions, type BrokerPositionsResponse } from "@/features/trading/api";

const QUERY_KEY = ["trading", "broker-positions"] as const;

export function useLiveBrokerPositions() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchBrokerPositions,
    refetchInterval: 15_000,
  });

  useEffect(() => {
    const source = new EventSource("/api/positions/stream");

    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as {
          type?: string;
          positions?: BrokerPositionsResponse["positions"];
        };
        if (!payload.positions) {
          return;
        }
        const incomingPositions = payload.positions;
        queryClient.setQueryData(QUERY_KEY, (current: ReturnType<typeof normalizeBrokerPositions> | undefined) => {
          if (!current || payload.type === "snapshot") {
            return normalizeBrokerPositions({ positions: incomingPositions });
          }

          const merged: NonNullable<BrokerPositionsResponse["positions"]> = Object.fromEntries(
            current.positions.map((position) => [
              position.positionKey,
              {
                position_key: position.positionKey,
                tradingsymbol: position.tradingSymbol,
                exchange: position.exchange,
                product: position.product,
                quantity: position.quantity,
                average_price: position.averagePrice,
                last_price: position.lastPrice,
                pnl: position.pnl,
                realized_pnl: position.realizedPnl,
                unrealized_pnl: position.unrealizedPnl,
              },
            ]),
          );

          for (const [key, value] of Object.entries(incomingPositions)) {
            merged[key] = value;
          }
          return normalizeBrokerPositions({ positions: merged });
        });
      } catch {
        // ignore malformed SSE payloads
      }
    };

    source.onerror = () => {
      source.close();
    };

    return () => source.close();
  }, [queryClient]);

  return query;
}
