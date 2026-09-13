/**
 * Central query-key factory so invalidation is never a stringly-typed guess.
 * Every key is prefixed with `alerts` so a mutation can invalidate the whole
 * feature with `alertsKeys.all` when it genuinely affects everything.
 */

export const alertsKeys = {
  all: ["alerts"] as const,

  scopes: () => [...alertsKeys.all, "scopes"] as const,

  capabilities: (scope: string | null) =>
    [...alertsKeys.all, "capabilities", scope] as const,

  workflows: (scope: string | null, includeArchived: boolean) =>
    [...alertsKeys.all, "workflows", scope, includeArchived] as const,

  workflow: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "workflow", workflowId, scope] as const,

  workflowHealth: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "workflow-health", workflowId, scope] as const,

  workflowRevisions: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "workflow-revisions", workflowId, scope] as const,

  workflowEvents: (workflowId: string, scope: string | null, limit: number, offset: number) =>
    [...alertsKeys.all, "workflow-events", workflowId, scope, limit, offset] as const,

  workflowDeliveries: (
    workflowId: string,
    scope: string | null,
    limit: number,
    offset: number,
    status: string | null,
  ) => [...alertsKeys.all, "workflow-deliveries", workflowId, scope, limit, offset, status] as const,

  channels: (scope: string | null) => [...alertsKeys.all, "channels", scope] as const,

  tokens: (scope: string | null) => [...alertsKeys.all, "tokens", scope] as const,

  tokenPresets: (scope: string | null) => [...alertsKeys.all, "token-presets", scope] as const,

  platformHealth: (scope: string | null) => [...alertsKeys.all, "platform-health", scope] as const,

  universes: (scope: string | null) => [...alertsKeys.all, "universes", scope] as const,

  universe: (name: string, scope: string | null) =>
    [...alertsKeys.all, "universe", name, scope] as const,

  universeRevisions: (name: string, scope: string | null) =>
    [...alertsKeys.all, "universe-revisions", name, scope] as const,

  screenerRuns: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "screener-runs", workflowId, scope] as const,

  screenerRun: (runId: string, scope: string | null) =>
    [...alertsKeys.all, "screener-run", runId, scope] as const,

  screenerAttachments: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "screener-attachments", workflowId, scope] as const,

  producers: (scope: string | null) => [...alertsKeys.all, "producers", scope] as const,

  producerCredentials: (producer: string, scope: string | null) =>
    [...alertsKeys.all, "producer-credentials", producer, scope] as const,

  signalValues: (producer: string, scope: string | null) =>
    [...alertsKeys.all, "signal-values", producer, scope] as const,

  signalsHealth: (scope: string | null) => [...alertsKeys.all, "signals-health", scope] as const,

  canvasLayout: (workflowId: string, scope: string | null) =>
    [...alertsKeys.all, "canvas-layout", workflowId, scope] as const,
};
