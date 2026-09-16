"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { alertsKeys } from "@/features/alerts/hooks/keys";
import {
  deleteAlertsWorkflow,
  activateAlertsWorkflow,
  archiveAlertsWorkflow,
  createAlertsProducer,
  createAlertsToken,
  createAlertsUniverse,
  deleteAlertsCanvasLayout,
  fetchAlertsCanvasLayout,
  fetchAlertsCapabilities,
  fetchAlertsChannels,
  fetchScreenerDataStatus,
  warmScreenerCandles,
  fetchAlertsPlatformHealth,
  fetchAlertsProducerCredentials,
  fetchAlertsProducers,
  fetchAlertsScopes,
  fetchAlertsScreenerAttachments,
  fetchAlertsScreenerRun,
  fetchAlertsScreenerRuns,
  fetchAlertsSignalValues,
  fetchAlertsSignalsHealth,
  fetchAlertsTokenPresets,
  fetchAlertsTokens,
  fetchAlertsUniverse,
  fetchAlertsUniverseRevisions,
  fetchAlertsUniverses,
  fetchAlertsWorkflow,
  fetchAlertsWorkflowDeliveries,
  fetchAlertsWorkflowEvents,
  fetchAlertsWorkflowHealth,
  fetchAlertsWorkflowRevisions,
  fetchAlertsWorkflows,
  issueAlertsProducerCredential,
  pauseAlertsWorkflow,
  previewAlertsUniverse,
  resolveAlertsUniverse,
  resumeAlertsWorkflow,
  setAlertsNotificationFrequency,
  revokeAlertsProducer,
  revokeAlertsProducerCredential,
  revokeAlertsToken,
  saveAlertsCanvasLayout,
  testAlertsChannel,
  triggerAlertsScreenerRun,
  upsertAlertsChannel,
} from "@/features/alerts/api";
import type { AlertsCanvasNode, AlertsScopeOption } from "@/features/alerts/types";

/**
 * The selected scope, held in the URL (`?scope=`) so a page is shareable and a
 * refresh keeps the operator where they were.
 *
 * The scope here is a SELECTION, never an authority: the server decides what
 * the operator may read and rejects anything else. Falling back to the
 * server's default scope means the first render shows real data instead of an
 * empty page.
 */
export function useAlertsScope() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const scopesQuery = useQuery({
    queryKey: alertsKeys.scopes(),
    queryFn: fetchAlertsScopes,
    staleTime: 5 * 60_000,
  });

  const scopes = useMemo<AlertsScopeOption[]>(
    () => scopesQuery.data?.scopes ?? [],
    [scopesQuery.data],
  );
  const requested = searchParams?.get("scope") ?? null;

  const resolvedScope = useMemo(() => {
    if (requested && scopes.some((option) => option.scope === requested)) {
      return requested;
    }
    const preferred = scopes.find((option) => option.has_data) ?? scopes.find((option) => option.is_default);
    return preferred?.scope ?? scopes[0]?.scope ?? null;
  }, [requested, scopes]);

  const setScope = useCallback(
    (next: string) => {
      const params = new URLSearchParams(searchParams?.toString() ?? "");
      params.set("scope", next);
      router.replace(`${pathname}?${params.toString()}`);
    },
    [pathname, router, searchParams],
  );

  return {
    scope: resolvedScope,
    scopes,
    setScope,
    isLoading: scopesQuery.isLoading,
    /** True when the scopes call itself failed, so pages can show an error. */
    isError: scopesQuery.isError,
    error: scopesQuery.error,
    /**
     * True when the requested scope was not authorized and we fell back.
     *
     * Gated on the scopes query having RESOLVED: while it is still loading the
     * authorized list is empty, so every `?scope=` would look unauthorized and
     * the "not authorized" banner would flash before the truth arrives.
     */
    fellBack:
      !scopesQuery.isLoading &&
      !scopesQuery.isError &&
      Boolean(requested) &&
      requested !== resolvedScope,
  };
}

export function useAlertsCapabilities(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.capabilities(scope),
    queryFn: () => fetchAlertsCapabilities(scope),
    enabled: Boolean(scope),
    staleTime: 5 * 60_000,
  });
}

export function useAlertsWorkflows(scope: string | null, includeArchived = false) {
  return useQuery({
    queryKey: alertsKeys.workflows(scope, includeArchived),
    queryFn: () => fetchAlertsWorkflows({ scope, includeArchived }),
    enabled: Boolean(scope),
    refetchInterval: 30_000,
  });
}

export function useAlertsWorkflow(
  workflowId: string,
  scope: string | null,
  options: { includeYaml?: boolean } = {},
) {
  return useQuery({
    queryKey: alertsKeys.workflow(workflowId, scope),
    queryFn: () => fetchAlertsWorkflow(workflowId, { scope, includeYaml: options.includeYaml }),
    enabled: Boolean(workflowId && scope),
  });
}

export function useAlertsWorkflowRevisions(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.workflowRevisions(workflowId, scope),
    queryFn: () => fetchAlertsWorkflowRevisions(workflowId, { scope }),
    enabled: Boolean(workflowId && scope),
  });
}

export function useAlertsWorkflowHealth(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.workflowHealth(workflowId, scope),
    queryFn: () => fetchAlertsWorkflowHealth(workflowId, scope),
    enabled: Boolean(workflowId && scope),
    refetchInterval: 15_000,
  });
}

export function useAlertsWorkflowEvents(
  workflowId: string,
  scope: string | null,
  limit = 50,
  offset = 0,
) {
  return useQuery({
    queryKey: alertsKeys.workflowEvents(workflowId, scope, limit, offset),
    queryFn: () => fetchAlertsWorkflowEvents(workflowId, { scope, limit, offset }),
    enabled: Boolean(workflowId && scope),
  });
}

export function useAlertsWorkflowDeliveries(
  workflowId: string,
  scope: string | null,
  limit = 50,
  offset = 0,
  status: string | null = null,
) {
  return useQuery({
    queryKey: alertsKeys.workflowDeliveries(workflowId, scope, limit, offset, status),
    queryFn: () => fetchAlertsWorkflowDeliveries(workflowId, { scope, limit, offset, status }),
    enabled: Boolean(workflowId && scope),
  });
}

/**
 * Lifecycle mutations. All four invalidate the list and the one workflow so the
 * operator's next read reflects the change without a manual refresh.
 */
export function useAlertsLifecycle(workflowId: string, scope: string | null) {
  const queryClient = useQueryClient();

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: alertsKeys.workflows(scope, false) });
    void queryClient.invalidateQueries({ queryKey: alertsKeys.workflows(scope, true) });
    void queryClient.invalidateQueries({ queryKey: alertsKeys.workflow(workflowId, scope) });
    void queryClient.invalidateQueries({ queryKey: alertsKeys.workflowHealth(workflowId, scope) });
  }, [queryClient, scope, workflowId]);

  return {
    activate: useMutation({
      mutationFn: (revision?: number | null) =>
        activateAlertsWorkflow(workflowId, { revision, scope }),
      onSuccess: invalidate,
    }),
    pause: useMutation({
      mutationFn: () => pauseAlertsWorkflow(workflowId, scope),
      onSuccess: invalidate,
    }),
    resume: useMutation({
      mutationFn: () => resumeAlertsWorkflow(workflowId, scope),
      onSuccess: invalidate,
    }),
    archive: useMutation({
      mutationFn: () => archiveAlertsWorkflow(workflowId, scope),
      onSuccess: invalidate,
    }),
    setFrequency: useMutation({
      mutationFn: (payload: {
        frequency: "once" | "repeated" | "reminder";
        reminder_interval_s?: number | null;
      }) => setAlertsNotificationFrequency(workflowId, payload, scope),
      onSuccess: invalidate,
    }),
    remove: useMutation({
      mutationFn: (keepHistory?: boolean) =>
        deleteAlertsWorkflow(workflowId, { scope, keepHistory }),
      onSuccess: invalidate,
    }),
  };
}

// ---------------------------------------------------------------------------
// universes
// ---------------------------------------------------------------------------

export function useAlertsUniverses(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.universes(scope),
    queryFn: () => fetchAlertsUniverses(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsUniverse(name: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.universe(name, scope),
    queryFn: () => fetchAlertsUniverse(name, scope),
    enabled: Boolean(name && scope),
  });
}

export function useAlertsUniverseRevisions(name: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.universeRevisions(name, scope),
    queryFn: () => fetchAlertsUniverseRevisions(name, { scope }),
    enabled: Boolean(name && scope),
  });
}

/**
 * Resolution writes a revision, so it is always an explicit operator action —
 * never a side effect of viewing. Preview is separate and persists nothing.
 */
export function useAlertsUniverseMutations(scope: string | null) {
  const queryClient = useQueryClient();

  const invalidate = (name?: string) => {
    void queryClient.invalidateQueries({ queryKey: alertsKeys.universes(scope) });
    if (name) {
      void queryClient.invalidateQueries({ queryKey: alertsKeys.universe(name, scope) });
      void queryClient.invalidateQueries({ queryKey: alertsKeys.universeRevisions(name, scope) });
    }
  };

  return {
    preview: useMutation({
      mutationFn: (payload: { kind: string; source_config?: Record<string, unknown> }) =>
        previewAlertsUniverse(payload, scope),
    }),
    resolve: useMutation({
      mutationFn: (name: string) => resolveAlertsUniverse(name, scope),
      onSuccess: (_data, name) => invalidate(name),
    }),
    create: useMutation({
      mutationFn: (payload: { name: string; kind: string; source_config?: Record<string, unknown> }) =>
        createAlertsUniverse(payload, scope),
      onSuccess: () => invalidate(),
    }),
  };
}

// ---------------------------------------------------------------------------
// screeners
// ---------------------------------------------------------------------------

export function useAlertsScreenerRuns(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.screenerRuns(workflowId, scope),
    queryFn: () => fetchAlertsScreenerRuns(workflowId, { scope }),
    enabled: Boolean(workflowId && scope),
    // A manual run completes asynchronously. Poll only while one is running, so
    // the operator sees running -> complete|partial|failed without a refresh,
    // and an idle screener is not re-fetched.
    refetchInterval: (query) =>
      (query.state.data?.runs ?? []).some((run) => run.status === "running") ? 5_000 : false,
  });
}

export function useAlertsScreenerRun(runId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.screenerRun(runId, scope),
    queryFn: () => fetchAlertsScreenerRun(runId, { scope }),
    enabled: Boolean(runId && scope),
    refetchInterval: (query) => (query.state.data?.run.status === "running" ? 5_000 : false),
  });
}

export function useAlertsScreenerAttachments(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.screenerAttachments(workflowId, scope),
    queryFn: () => fetchAlertsScreenerAttachments(workflowId, { scope }),
    enabled: Boolean(workflowId && scope),
  });
}

/**
 * Candle availability for a screener's members.
 *
 * Polls only while something still needs candles, because that is the only state
 * that changes on its own; a complete universe is fetched once.
 */
export function useAlertsScreenerDataStatus(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.screenerDataStatus(workflowId, scope),
    queryFn: () => fetchScreenerDataStatus(workflowId, scope),
    enabled: Boolean(scope && workflowId),
    refetchInterval: (query) =>
      (query.state.data?.members_needing_candles ?? 0) > 0 ? 20_000 : false,
  });
}

export function useAlertsScreenerWarm(workflowId: string, scope: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => warmScreenerCandles(workflowId, scope),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: alertsKeys.screenerDataStatus(workflowId, scope),
      });
    },
  });
}

export function useAlertsScreenerMutations(workflowId: string, scope: string | null) {
  const queryClient = useQueryClient();
  return {
    trigger: useMutation({
      mutationFn: (idempotencyKey: string) =>
        triggerAlertsScreenerRun(workflowId, { idempotencyKey, scope }),
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: alertsKeys.screenerRuns(workflowId, scope) });
        void queryClient.invalidateQueries({
          queryKey: alertsKeys.screenerAttachments(workflowId, scope),
        });
      },
    }),
  };
}

// ---------------------------------------------------------------------------
// operations: channels, tokens, producers, health
// ---------------------------------------------------------------------------

export function useAlertsChannels(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.channels(scope),
    queryFn: () => fetchAlertsChannels(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsChannelMutations(scope: string | null) {
  const queryClient = useQueryClient();
  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: alertsKeys.channels(scope) });
  return {
    upsert: useMutation({
      mutationFn: (payload: {
        name: string;
        provider: string;
        destination?: Record<string, unknown>;
        secret_env?: string | null;
        enabled?: boolean;
      }) => upsertAlertsChannel(payload, scope),
      onSuccess: invalidate,
    }),
    // Sends a REAL message; always a deliberate, separate action.
    test: useMutation({
      mutationFn: (channelId: string) => testAlertsChannel(channelId, {}, scope),
    }),
  };
}

export function useAlertsTokens(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.tokens(scope),
    queryFn: () => fetchAlertsTokens(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsTokenPresets(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.tokenPresets(scope),
    queryFn: () => fetchAlertsTokenPresets(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsTokenMutations(scope: string | null) {
  const queryClient = useQueryClient();
  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: alertsKeys.tokens(scope) });
  return {
    create: useMutation({
      mutationFn: (payload: {
        label: string;
        preset?: string;
        allowed_actions?: string[];
        allowed_modes?: string[];
      }) => createAlertsToken(payload, scope),
      onSuccess: invalidate,
    }),
    revoke: useMutation({
      mutationFn: (tokenId: string) => revokeAlertsToken(tokenId, scope),
      onSuccess: invalidate,
    }),
  };
}

export function useAlertsProducers(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.producers(scope),
    queryFn: () => fetchAlertsProducers(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsProducerCredentials(producer: string | null, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.producerCredentials(producer ?? "", scope),
    queryFn: () => fetchAlertsProducerCredentials(producer as string, scope),
    enabled: Boolean(producer && scope),
  });
}

export function useAlertsSignalValues(
  producer: string | null,
  scope: string | null,
  limit = 20,
) {
  return useQuery({
    queryKey: [...alertsKeys.signalValues(producer ?? "", scope), limit] as const,
    queryFn: () => fetchAlertsSignalValues(producer as string, { scope, limit }),
    enabled: Boolean(producer && scope),
  });
}

export function useAlertsSignalsHealth(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.signalsHealth(scope),
    queryFn: () => fetchAlertsSignalsHealth(scope),
    enabled: Boolean(scope),
  });
}

export function useAlertsProducerMutations(scope: string | null) {
  const queryClient = useQueryClient();
  const invalidate = (producer?: string) => {
    void queryClient.invalidateQueries({ queryKey: alertsKeys.producers(scope) });
    void queryClient.invalidateQueries({ queryKey: alertsKeys.signalsHealth(scope) });
    if (producer) {
      void queryClient.invalidateQueries({
        queryKey: alertsKeys.producerCredentials(producer, scope),
      });
    }
  };
  return {
    create: useMutation({
      mutationFn: (payload: {
        name: string;
        value_schema?: Record<string, unknown> | null;
        default_ttl_s?: number | null;
      }) => createAlertsProducer(payload, scope),
      onSuccess: () => invalidate(),
    }),
    revoke: useMutation({
      mutationFn: (name: string) => revokeAlertsProducer(name, scope),
      onSuccess: (_data, name) => invalidate(name),
    }),
    issueCredential: useMutation({
      mutationFn: (name: string) => issueAlertsProducerCredential(name, scope),
      onSuccess: (_data, name) => invalidate(name),
    }),
    revokeCredential: useMutation({
      mutationFn: ({ name, tokenId }: { name: string; tokenId: string }) =>
        revokeAlertsProducerCredential(name, tokenId, scope),
      onSuccess: (_data, { name }) => invalidate(name),
    }),
  };
}

export function useAlertsPlatformHealth(scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.platformHealth(scope),
    queryFn: () => fetchAlertsPlatformHealth(scope),
    enabled: Boolean(scope),
  });
}

// ---------------------------------------------------------------------------
// canvas layout
// ---------------------------------------------------------------------------

export function useAlertsCanvasLayout(workflowId: string, scope: string | null) {
  return useQuery({
    queryKey: alertsKeys.canvasLayout(workflowId, scope),
    queryFn: () => fetchAlertsCanvasLayout(workflowId, scope),
    enabled: Boolean(workflowId && scope),
  });
}

/**
 * Layout mutations. Neither touches the workflow document: saving positions
 * cannot create a revision or move a canonical hash, which is the whole reason
 * layout lives in its own table.
 */
export function useAlertsCanvasMutations(workflowId: string, scope: string | null) {
  const queryClient = useQueryClient();
  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: alertsKeys.canvasLayout(workflowId, scope) });
  return {
    saveLayout: useMutation({
      mutationFn: (nodes: AlertsCanvasNode[]) => saveAlertsCanvasLayout(workflowId, nodes, scope),
      onSuccess: invalidate,
    }),
    deleteLayout: useMutation({
      mutationFn: (nodeIds: string[]) => deleteAlertsCanvasLayout(workflowId, nodeIds, scope),
      onSuccess: invalidate,
    }),
  };
}
