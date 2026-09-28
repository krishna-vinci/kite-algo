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

export type OptionsSession = {
  underlying: string;
  running: boolean;
  always_on: boolean;
  last_used_age_s: number | null;
  updated_age_s: number | null;
  desired_tokens: number;
  cadence_sec: number;
};

export type OptionsSettings = {
  always_on: string[];
  available_underlyings: string[];
  cadence_sec: number;
  tick_driven: boolean;
  min_interval_sec: number;
  idle_stop_minutes: number;
  source: "db" | "default";
  updated_at: string | null;
  updated_by: string | null;
  sessions: OptionsSession[];
};

export type OptionsSettingsPayload = {
  always_on: string[];
  cadence_sec: number;
  tick_driven: boolean;
  min_interval_sec: number;
  idle_stop_minutes: number;
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
