/**
 * Typed wrappers over the Phase 6 operator API (`/api/alerts/*`).
 *
 * One function per verified endpoint. Nothing here invents a route: the
 * operator surface deliberately has no `POST /signals/values` (submitting a
 * value needs a producer credential, not a browser session) and no
 * `signals/health?purge`.
 *
 * Mutations rely on `apiFetch` propagating the session cookie; the server
 * enforces a same-origin assertion on every unsafe method, so callers must not
 * need to add CSRF headers themselves.
 */

import { apiFetch } from "@/lib/api/client";
import type {
  AlertsCanvasLayoutResponse,
  AlertsCanvasNode,
  AlertsCapabilitiesResponse,
  AlertsChannelTestResponse,
  AlertsChannelsResponse,
  AlertsDeliveriesResponse,
  AlertsEventsResponse,
  AlertsInstrumentSearchResponse,
  AlertsLifecycleResponse,
  AlertsPlatformHealthResponse,
  AlertsPreviewResponse,
  AlertsProducerCredentialResponse,
  AlertsProducersResponse,
  AlertsScopesResponse,
  AlertsScreenerAttachmentsResponse,
  AlertsScreenerRunResponse,
  AlertsScreenerRunsResponse,
  AlertsSignalsHealthResponse,
  AlertsSignalValuesResponse,
  AlertsTokenCreateResponse,
  AlertsTokenPresetsResponse,
  AlertsTokensResponse,
  AlertsUniverseDetailResponse,
  AlertsUniversePreviewResponse,
  AlertsUniverseResolveResponse,
  AlertsUniverseRevisionsResponse,
  AlertsUniversesResponse,
  AlertsValidateResponse,
  AlertsWorkflowDetailResponse,
  AlertsWorkflowHealthResponse,
  AlertsWorkflowListResponse,
  AlertsWorkflowMutationResponse,
  AlertsWorkflowRevisionsResponse,
} from "@/features/alerts/types";

const BASE = "/api/alerts";

function scopeQuery(scope?: string | null): string {
  return scope ? `?scope=${encodeURIComponent(scope)}` : "";
}

function withQuery(path: string, params?: Record<string, string | number | boolean | null | undefined>): string {
  if (!params) return path;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

// ---------------------------------------------------------------------------
// scopes / capabilities / instruments
// ---------------------------------------------------------------------------

export async function fetchAlertsScopes(): Promise<AlertsScopesResponse> {
  return apiFetch<AlertsScopesResponse>(`${BASE}/scopes`);
}

export async function fetchAlertsCapabilities(scope?: string | null): Promise<AlertsCapabilitiesResponse> {
  return apiFetch<AlertsCapabilitiesResponse>(`${BASE}/capabilities${scopeQuery(scope)}`);
}

export async function searchAlertsInstruments(params: {
  q: string;
  exchange?: string | null;
  segment?: string | null;
  limit?: number;
}): Promise<AlertsInstrumentSearchResponse> {
  return apiFetch<AlertsInstrumentSearchResponse>(
    withQuery(`${BASE}/instruments/search`, { ...params }),
  );
}

// ---------------------------------------------------------------------------
// workflows
// ---------------------------------------------------------------------------

export async function fetchAlertsWorkflows(params: {
  scope?: string | null;
  includeArchived?: boolean;
} = {}): Promise<AlertsWorkflowListResponse> {
  return apiFetch<AlertsWorkflowListResponse>(
    withQuery(`${BASE}/workflows`, {
      scope: params.scope,
      include_archived: params.includeArchived ? "true" : "false",
    }),
  );
}

export async function fetchAlertsWorkflow(
  workflowId: string,
  params: { scope?: string | null; includeYaml?: boolean } = {},
): Promise<AlertsWorkflowDetailResponse> {
  const search = new URLSearchParams();
  if (params.scope) search.set("scope", params.scope);
  if (params.includeYaml === false) search.set("include_yaml", "false");
  const qs = search.toString();
  return apiFetch<AlertsWorkflowDetailResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}${qs ? `?${qs}` : ""}`,
  );
}

export async function validateAlertsWorkflow(
  payload: { document?: Record<string, unknown>; yaml_text?: string },
  scope?: string | null,
): Promise<AlertsValidateResponse> {
  return apiFetch<AlertsValidateResponse>(`${BASE}/workflows/validate${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function previewAlertsWorkflow(
  payload: { document?: Record<string, unknown>; yaml_text?: string; observations?: unknown[] },
  scope?: string | null,
): Promise<AlertsPreviewResponse> {
  return apiFetch<AlertsPreviewResponse>(`${BASE}/workflows/preview${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function createAlertsWorkflow(
  payload: { name?: string; document?: Record<string, unknown>; yaml_text?: string; idempotency_key?: string },
  scope?: string | null,
): Promise<AlertsWorkflowMutationResponse> {
  return apiFetch<AlertsWorkflowMutationResponse>(`${BASE}/workflows${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function patchAlertsWorkflow(
  workflowId: string,
  payload: {
    document?: Record<string, unknown>;
    yaml_text?: string;
    expected_revision: number;
  },
  scope?: string | null,
): Promise<AlertsWorkflowMutationResponse> {
  return apiFetch<AlertsWorkflowMutationResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}${scopeQuery(scope)}`,
    { method: "PATCH", json: payload },
  );
}

export async function activateAlertsWorkflow(
  workflowId: string,
  params: { revision?: number | null; scope?: string | null } = {},
): Promise<AlertsLifecycleResponse> {
  const search = new URLSearchParams();
  if (params.revision != null) search.set("revision", String(params.revision));
  if (params.scope) search.set("scope", params.scope);
  const qs = search.toString();
  return apiFetch<AlertsLifecycleResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/activate${qs ? `?${qs}` : ""}`,
    { method: "POST" },
  );
}

export async function pauseAlertsWorkflow(
  workflowId: string,
  scope?: string | null,
): Promise<AlertsLifecycleResponse> {
  return apiFetch<AlertsLifecycleResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/pause${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

export async function resumeAlertsWorkflow(
  workflowId: string,
  scope?: string | null,
): Promise<AlertsLifecycleResponse> {
  return apiFetch<AlertsLifecycleResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/resume${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

export async function archiveAlertsWorkflow(
  workflowId: string,
  scope?: string | null,
): Promise<AlertsLifecycleResponse> {
  return apiFetch<AlertsLifecycleResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/archive${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

export async function fetchAlertsWorkflowRevisions(
  workflowId: string,
  params: { scope?: string | null; limit?: number } = {},
): Promise<AlertsWorkflowRevisionsResponse> {
  return apiFetch<AlertsWorkflowRevisionsResponse>(
    withQuery(`${BASE}/workflows/${encodeURIComponent(workflowId)}/revisions`, {
      scope: params.scope,
      limit: params.limit ?? 50,
    }),
  );
}

export async function fetchAlertsWorkflowYaml(
  workflowId: string,
  params: { revision?: number | null; scope?: string | null } = {},
): Promise<{
  ok: boolean;
  workflow_id: string;
  revision: number;
  revision_id: string;
  revision_status: string;
  canonical_hash: string;
  yaml: string;
  round_trip: string;
}> {
  const search = new URLSearchParams();
  if (params.revision != null) search.set("revision", String(params.revision));
  if (params.scope) search.set("scope", params.scope);
  const qs = search.toString();
  return apiFetch(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/yaml${qs ? `?${qs}` : ""}`,
  );
}

export async function fetchAlertsWorkflowHealth(
  workflowId: string,
  scope?: string | null,
): Promise<AlertsWorkflowHealthResponse> {
  return apiFetch<AlertsWorkflowHealthResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/health${scopeQuery(scope)}`,
  );
}

export async function fetchAlertsWorkflowEvents(
  workflowId: string,
  params: { scope?: string | null; limit?: number; offset?: number } = {},
): Promise<AlertsEventsResponse> {
  return apiFetch<AlertsEventsResponse>(
    withQuery(`${BASE}/workflows/${encodeURIComponent(workflowId)}/events`, {
      scope: params.scope,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
    }),
  );
}

export async function fetchAlertsWorkflowDeliveries(
  workflowId: string,
  params: { scope?: string | null; limit?: number; offset?: number; status?: string | null } = {},
): Promise<AlertsDeliveriesResponse> {
  return apiFetch<AlertsDeliveriesResponse>(
    withQuery(`${BASE}/workflows/${encodeURIComponent(workflowId)}/deliveries`, {
      scope: params.scope,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
      status: params.status,
    }),
  );
}

export async function fetchAlertsPlatformHealth(
  scope?: string | null,
): Promise<AlertsPlatformHealthResponse> {
  return apiFetch<AlertsPlatformHealthResponse>(`${BASE}/health${scopeQuery(scope)}`);
}

// ---------------------------------------------------------------------------
// channels
// ---------------------------------------------------------------------------

export async function fetchAlertsChannels(scope?: string | null): Promise<AlertsChannelsResponse> {
  return apiFetch<AlertsChannelsResponse>(`${BASE}/channels${scopeQuery(scope)}`);
}

export async function upsertAlertsChannel(
  payload: {
    name: string;
    provider: string;
    destination?: Record<string, unknown>;
    secret_env?: string | null;
    enabled?: boolean;
  },
  scope?: string | null,
): Promise<{ ok: boolean; channel: AlertsChannelsResponse["channels"][number] }> {
  return apiFetch(`${BASE}/channels${scopeQuery(scope)}`, { method: "POST", json: payload });
}

/**
 * Send a REAL test message. Deliberately a separate, explicit action — it is
 * the only operator route that contacts a provider.
 */
export async function testAlertsChannel(
  channelId: string,
  payload: { message?: string } = {},
  scope?: string | null,
): Promise<AlertsChannelTestResponse> {
  return apiFetch<AlertsChannelTestResponse>(
    `${BASE}/channels/${encodeURIComponent(channelId)}/test${scopeQuery(scope)}`,
    { method: "POST", json: payload },
  );
}

// ---------------------------------------------------------------------------
// tokens
// ---------------------------------------------------------------------------

export async function fetchAlertsTokenPresets(scope?: string | null): Promise<AlertsTokenPresetsResponse> {
  return apiFetch<AlertsTokenPresetsResponse>(`${BASE}/tokens/presets${scopeQuery(scope)}`);
}

export async function fetchAlertsTokens(scope?: string | null): Promise<AlertsTokensResponse> {
  return apiFetch<AlertsTokensResponse>(`${BASE}/tokens${scopeQuery(scope)}`);
}

export async function createAlertsToken(
  payload: { label: string; preset?: string; allowed_actions?: string[]; allowed_modes?: string[] },
  scope?: string | null,
): Promise<AlertsTokenCreateResponse> {
  return apiFetch<AlertsTokenCreateResponse>(`${BASE}/tokens${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function revokeAlertsToken(
  tokenId: string,
  scope?: string | null,
): Promise<{ ok: boolean; token: unknown }> {
  return apiFetch(`${BASE}/tokens/${encodeURIComponent(tokenId)}/revoke${scopeQuery(scope)}`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// universes
// ---------------------------------------------------------------------------

export async function fetchAlertsUniverses(scope?: string | null): Promise<AlertsUniversesResponse> {
  return apiFetch<AlertsUniversesResponse>(`${BASE}/universes${scopeQuery(scope)}`);
}

export async function fetchAlertsUniverse(
  name: string,
  scope?: string | null,
): Promise<AlertsUniverseDetailResponse> {
  return apiFetch<AlertsUniverseDetailResponse>(
    `${BASE}/universes/${encodeURIComponent(name)}${scopeQuery(scope)}`,
  );
}

export async function fetchAlertsUniverseRevisions(
  name: string,
  params: { scope?: string | null; limit?: number } = {},
): Promise<AlertsUniverseRevisionsResponse> {
  return apiFetch<AlertsUniverseRevisionsResponse>(
    withQuery(`${BASE}/universes/${encodeURIComponent(name)}/revisions`, {
      scope: params.scope,
      limit: params.limit ?? 50,
    }),
  );
}

export async function createAlertsUniverse(
  payload: { name: string; kind: string; source_config?: Record<string, unknown> },
  scope?: string | null,
): Promise<{ ok: boolean; universe_id: string; name: string; kind: string }> {
  return apiFetch(`${BASE}/universes${scopeQuery(scope)}`, { method: "POST", json: payload });
}

export async function previewAlertsUniverse(
  payload: { kind: string; source_config?: Record<string, unknown> },
  scope?: string | null,
): Promise<AlertsUniversePreviewResponse> {
  return apiFetch<AlertsUniversePreviewResponse>(`${BASE}/universes/preview${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function resolveAlertsUniverse(
  name: string,
  scope?: string | null,
): Promise<AlertsUniverseResolveResponse> {
  return apiFetch<AlertsUniverseResolveResponse>(
    `${BASE}/universes/${encodeURIComponent(name)}/resolve${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// screeners
// ---------------------------------------------------------------------------

export async function fetchAlertsScreenerRuns(
  workflowId: string,
  params: { scope?: string | null; limit?: number; offset?: number } = {},
): Promise<AlertsScreenerRunsResponse> {
  return apiFetch<AlertsScreenerRunsResponse>(
    withQuery(`${BASE}/screeners/${encodeURIComponent(workflowId)}/runs`, {
      scope: params.scope,
      limit: params.limit ?? 20,
      offset: params.offset ?? 0,
    }),
  );
}

export async function fetchAlertsScreenerRun(
  runId: string,
  params: { scope?: string | null; limit?: number; offset?: number } = {},
): Promise<AlertsScreenerRunResponse> {
  return apiFetch<AlertsScreenerRunResponse>(
    withQuery(`${BASE}/screener-runs/${encodeURIComponent(runId)}`, {
      scope: params.scope,
      limit: params.limit ?? 100,
      offset: params.offset ?? 0,
    }),
  );
}

export async function triggerAlertsScreenerRun(
  workflowId: string,
  params: { idempotencyKey?: string | null; scope?: string | null } = {},
): Promise<{ ok: boolean; run_id: string | null; status: string; detail?: string }> {
  return apiFetch(
    withQuery(`${BASE}/screeners/${encodeURIComponent(workflowId)}/runs`, {
      idempotency_key: params.idempotencyKey,
      scope: params.scope,
    }),
    { method: "POST" },
  );
}

export async function fetchAlertsScreenerAttachments(
  workflowId: string,
  params: { revision?: number | null; scope?: string | null } = {},
): Promise<AlertsScreenerAttachmentsResponse> {
  return apiFetch<AlertsScreenerAttachmentsResponse>(
    withQuery(`${BASE}/screeners/${encodeURIComponent(workflowId)}/attachments`, {
      revision: params.revision,
      scope: params.scope,
    }),
  );
}

// ---------------------------------------------------------------------------
// external producers
// ---------------------------------------------------------------------------

export async function fetchAlertsProducers(scope?: string | null): Promise<AlertsProducersResponse> {
  return apiFetch<AlertsProducersResponse>(`${BASE}/signals/producers${scopeQuery(scope)}`);
}

export async function fetchAlertsProducer(
  name: string,
  scope?: string | null,
): Promise<{ ok: boolean; producer: AlertsProducersResponse["producers"][number] }> {
  return apiFetch(`${BASE}/signals/producers/${encodeURIComponent(name)}${scopeQuery(scope)}`);
}

export async function createAlertsProducer(
  payload: { name: string; value_schema?: Record<string, unknown> | null; default_ttl_s?: number | null },
  scope?: string | null,
): Promise<{ ok: boolean; producer: AlertsProducersResponse["producers"][number] }> {
  return apiFetch(`${BASE}/signals/producers${scopeQuery(scope)}`, {
    method: "POST",
    json: payload,
  });
}

export async function revokeAlertsProducer(
  name: string,
  scope?: string | null,
): Promise<{ ok: boolean; producer: AlertsProducersResponse["producers"][number]; note: string }> {
  return apiFetch(`${BASE}/signals/producers/${encodeURIComponent(name)}/revoke${scopeQuery(scope)}`, {
    method: "POST",
  });
}

/** Issue a producer credential. The secret is returned EXACTLY ONCE. */
export async function issueAlertsProducerCredential(
  name: string,
  scope?: string | null,
): Promise<AlertsProducerCredentialResponse> {
  return apiFetch<AlertsProducerCredentialResponse>(
    `${BASE}/signals/producers/${encodeURIComponent(name)}/credentials${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

export async function revokeAlertsProducerCredential(
  name: string,
  tokenId: string,
  scope?: string | null,
): Promise<{ ok: boolean; token_id: string; revoked: boolean }> {
  return apiFetch(
    `${BASE}/signals/producers/${encodeURIComponent(name)}/credentials/${encodeURIComponent(tokenId)}/revoke${scopeQuery(scope)}`,
    { method: "POST" },
  );
}

export async function fetchAlertsSignalValues(
  producer: string,
  params: { scope?: string | null; limit?: number; offset?: number } = {},
): Promise<AlertsSignalValuesResponse> {
  return apiFetch<AlertsSignalValuesResponse>(
    withQuery(`${BASE}/signals/values`, {
      producer,
      scope: params.scope,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
    }),
  );
}

export async function fetchAlertsSignalsHealth(scope?: string | null): Promise<AlertsSignalsHealthResponse> {
  return apiFetch<AlertsSignalsHealthResponse>(`${BASE}/signals/health${scopeQuery(scope)}`);
}

// ---------------------------------------------------------------------------
// canvas layout
// ---------------------------------------------------------------------------

export async function fetchAlertsCanvasLayout(
  workflowId: string,
  scope?: string | null,
): Promise<AlertsCanvasLayoutResponse> {
  return apiFetch<AlertsCanvasLayoutResponse>(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/layout${scopeQuery(scope)}`,
  );
}

export async function saveAlertsCanvasLayout(
  workflowId: string,
  nodes: AlertsCanvasNode[],
  scope?: string | null,
): Promise<{ ok: boolean; workflow_id: string; saved: number }> {
  return apiFetch(`${BASE}/workflows/${encodeURIComponent(workflowId)}/layout${scopeQuery(scope)}`, {
    method: "PUT",
    json: { nodes },
  });
}

export async function deleteAlertsCanvasLayout(
  workflowId: string,
  nodeIds: string[],
  scope?: string | null,
): Promise<{ ok: boolean; workflow_id: string; removed: number }> {
  return apiFetch(
    `${BASE}/workflows/${encodeURIComponent(workflowId)}/layout/delete${scopeQuery(scope)}`,
    { method: "POST", json: { node_ids: nodeIds } },
  );
}
