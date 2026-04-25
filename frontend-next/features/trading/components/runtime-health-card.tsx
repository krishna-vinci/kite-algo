"use client";

import type { RuntimeStatus } from "@/components/options/types";
import { StatusBadge } from "@/components/operator/status-badge";
import { Panel } from "@/components/operator/panel";

type RuntimeHealthCardProps = {
  runtime: RuntimeStatus;
};

function brokerTone(status: RuntimeStatus["brokerStatus"]) {
  if (status === "connected") return "positive" as const;
  if (status === "reconnecting" || status === "degraded") return "warning" as const;
  if (status === "disconnected") return "danger" as const;
  return "neutral" as const;
}

function wsTone(status: string) {
  if (status === "connected" || status === "active") return "positive" as const;
  if (status === "reconnecting" || status === "degraded") return "warning" as const;
  return "neutral" as const;
}

export function RuntimeHealthCard({ runtime }: RuntimeHealthCardProps) {
  return (
    <Panel eyebrow="runtime" title="System health">
      <div className="grid gap-2 sm:grid-cols-3">
        <div className="flex items-center justify-between rounded-lg border border-border/50 bg-background/50 px-3 py-2">
          <span className="text-xs text-foreground/50">Broker</span>
          <StatusBadge tone={brokerTone(runtime.brokerStatus)}>{runtime.brokerStatus}</StatusBadge>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border/50 bg-background/50 px-3 py-2">
          <span className="text-xs text-foreground/50">WebSocket</span>
          <StatusBadge tone={wsTone(runtime.websocketStatus)}>{runtime.websocketStatus}</StatusBadge>
        </div>
        <div className="flex items-center justify-between rounded-lg border border-border/50 bg-background/50 px-3 py-2">
          <span className="text-xs text-foreground/50">Paper engine</span>
          <StatusBadge tone={runtime.paperAvailable ? "positive" : "neutral"}>
            {runtime.paperAvailable ? "available" : "offline"}
          </StatusBadge>
        </div>
      </div>
    </Panel>
  );
}
