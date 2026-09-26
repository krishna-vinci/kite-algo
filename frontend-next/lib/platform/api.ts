/**
 * Typed wrappers over the platform-level live/paper control plane
 * (`/api/platform/*`). Mutations rely on `apiFetch` propagating the session
 * cookie; the server enforces same-origin on unsafe methods.
 */

import { apiFetch } from "@/lib/api/client";
import type {
  PlatformLiveSettings,
  PlatformLiveSettingsPayload,
  PlatformStatus,
} from "@/lib/platform/types";

const BASE = "/api/platform";

export async function fetchPlatformStatus(): Promise<PlatformStatus> {
  return apiFetch<PlatformStatus>(`${BASE}/status`);
}

export async function fetchPlatformLiveSettings(): Promise<PlatformLiveSettings> {
  return apiFetch<PlatformLiveSettings>(`${BASE}/live-settings`);
}

export async function updatePlatformLiveSettings(
  payload: PlatformLiveSettingsPayload,
): Promise<PlatformLiveSettings> {
  return apiFetch<PlatformLiveSettings>(`${BASE}/live-settings`, {
    method: "PUT",
    json: payload,
  });
}
