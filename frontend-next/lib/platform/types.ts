/**
 * Types for the platform-level live/paper control plane (`/api/platform/*`).
 * See the shared P2-UX API contract for the exact response shapes.
 */

export type PlatformLaneKey = "cnc" | "mis" | "futures" | "options";

export type PlatformLiveLanes = Record<PlatformLaneKey, boolean>;

export type PlatformLiveSettings = {
  live_enabled: boolean;
  lanes: PlatformLiveLanes;
  lanes_source: "db" | "env";
  account: { scope: string; allowed: boolean };
  updated_at: string | null;
  updated_by: string | null;
};

export type PlatformLiveSettingsPayload = {
  lanes: PlatformLiveLanes;
  reason: string;
};

export type PlatformComponentState = "ok" | "stale" | "down" | "unknown" | "connected" | "expired" | string;

export type PlatformStatus = {
  mode: "live" | "paper";
  broker: { state: PlatformComponentState; detail: string | null };
  market_data: { state: PlatformComponentState; last_tick_age_s: number | null };
  strategy_runner: { state: PlatformComponentState; last_seen_age_s: number | null };
  live: { enabled: boolean; lanes_open: string[] };
};
