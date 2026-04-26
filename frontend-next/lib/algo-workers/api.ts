import { apiFetch } from "@/lib/api/client";

export type AlgoWorkerToken = {
  tokenId: string;
  name: string;
  accountScope: string | null;
  allowedModes: string[];
  allowedActions: string[];
  allowedTemplates: string[];
  status: string;
  createdAt: string | null;
  expiresAt: string | null;
  lastUsedAt: string | null;
};

export type CreateAlgoWorkerTokenPayload = {
  name: string;
  accountScope?: string | null;
  allowedModes?: string[];
  allowedActions?: string[];
  allowedTemplates?: string[];
  expiresAt?: string | null;
};

export type CreatedAlgoWorkerToken = AlgoWorkerToken & {
  token: string;
};

export type KiteProfile = {
  userId: string | null;
  userName: string | null;
  raw: Record<string, unknown>;
};

const DEFAULT_ALLOWED_MODES = ["paper", "dry_run"];
const DEFAULT_ALLOWED_ACTIONS = [
  "heartbeat",
  "runs:create",
  "runs:read",
  "intents:submit",
  "risk:update",
  "runs:exit",
  "funds:read",
];

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function normalizeToken(raw: Record<string, unknown>): AlgoWorkerToken {
  return {
    tokenId: String(raw.token_id ?? ""),
    name: String(raw.name ?? "Algo worker"),
    accountScope: typeof raw.account_scope === "string" ? raw.account_scope : null,
    allowedModes: asStringArray(raw.allowed_modes),
    allowedActions: asStringArray(raw.allowed_actions),
    allowedTemplates: asStringArray(raw.allowed_templates),
    status: String(raw.status ?? "active"),
    createdAt: typeof raw.created_at === "string" ? raw.created_at : null,
    expiresAt: typeof raw.expires_at === "string" ? raw.expires_at : null,
    lastUsedAt: typeof raw.last_used_at === "string" ? raw.last_used_at : null,
  };
}

export async function listAlgoWorkerTokens(): Promise<AlgoWorkerToken[]> {
  const response = await apiFetch<Array<Record<string, unknown>>>("/api/algo-workers/tokens");
  return response.map(normalizeToken);
}

export async function createAlgoWorkerToken(payload: CreateAlgoWorkerTokenPayload): Promise<CreatedAlgoWorkerToken> {
  const response = await apiFetch<Record<string, unknown>>("/api/algo-workers/tokens", {
    method: "POST",
    json: {
      name: payload.name,
      account_scope: payload.accountScope || null,
      allowed_modes: payload.allowedModes?.length ? payload.allowedModes : DEFAULT_ALLOWED_MODES,
      allowed_actions: payload.allowedActions?.length ? payload.allowedActions : DEFAULT_ALLOWED_ACTIONS,
      allowed_templates: payload.allowedTemplates ?? [],
      expires_at: payload.expiresAt || null,
    },
  });
  return {
    ...normalizeToken(response),
    token: String(response.token ?? ""),
  };
}

export async function getKiteProfile(): Promise<KiteProfile> {
  const response = await apiFetch<Record<string, unknown>>("/api/profile_kite");
  return {
    userId: typeof response.user_id === "string" && response.user_id.trim() ? response.user_id.trim() : null,
    userName: typeof response.user_name === "string" && response.user_name.trim() ? response.user_name.trim() : null,
    raw: response,
  };
}

export async function revokeAlgoWorkerToken(tokenId: string): Promise<AlgoWorkerToken> {
  const response = await apiFetch<Record<string, unknown>>(`/api/algo-workers/tokens/${encodeURIComponent(tokenId)}/revoke`, {
    method: "POST",
  });
  return normalizeToken(response);
}
