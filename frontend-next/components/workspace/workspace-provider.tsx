"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { fetchJournalEnvironments } from "@/lib/journal/api";
import type { JournalEnvironment } from "@/lib/journal/types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WorkspaceMode = "live" | "paper";

export type WorkspaceContextValue = {
  environments: JournalEnvironment[];
  environmentsLoading: boolean;
  environmentsError: string | null;
  selectedMode: WorkspaceMode;
  selectedEnvironmentId: string;
  selectedEnvironment: JournalEnvironment | null;
  setSelectedMode: (value: WorkspaceMode) => void;
  setSelectedEnvironmentId: (value: string) => void;
};

// ---------------------------------------------------------------------------
// Session storage keys (namespaced to avoid collisions with old journal keys)
// ---------------------------------------------------------------------------

const SESSION_KEY_ENV = "workspace.v1.environment_id";
const SESSION_KEY_MODE = "workspace.v1.mode";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getInitialEnvironmentId(): string {
  if (typeof window === "undefined") return "";
  const params = new URLSearchParams(window.location.search);
  // Prefer `env` param, fall back to legacy `environment_id`, then sessionStorage
  const fromUrl =
    params.get("env") ??
    params.get("environment_id") ??
    "";
  if (fromUrl) return fromUrl;
  return window.sessionStorage.getItem(SESSION_KEY_ENV) ?? "";
}

function getInitialMode(): WorkspaceMode {
  if (typeof window === "undefined") return "live";
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("mode");
  if (fromUrl === "live" || fromUrl === "paper") return fromUrl;
  const fromSession = window.sessionStorage.getItem(SESSION_KEY_MODE);
  return fromSession === "paper" ? "paper" : "live";
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [environments, setEnvironments] = useState<JournalEnvironment[]>([]);
  const [environmentsLoading, setEnvironmentsLoading] = useState(true);
  const [environmentsError, setEnvironmentsError] = useState<string | null>(null);
  const [selectedModeState, setSelectedModeState] = useState<WorkspaceMode>(getInitialMode);
  const [selectedEnvironmentId, setSelectedEnvironmentIdState] = useState(getInitialEnvironmentId);

  // Fetch environments once on mount
  useEffect(() => {
    let closed = false;

    fetchJournalEnvironments()
      .then((items) => {
        if (closed) return;
        setEnvironments(items);
        setEnvironmentsError(null);

        // Auto-select: if no environment selected yet, pick the first one
        // matching the current mode preference, or the first one overall.
        setSelectedEnvironmentIdState((prev) => {
          if (prev) return prev; // already selected
          // First, try to match by current mode
          const mode = getInitialMode();
          const matching = items.find(
            (e) => (e.mode === "live" ? "live" : "paper") === mode,
          );
          if (matching) {
            // Persist to session storage
            if (typeof window !== "undefined") {
              window.sessionStorage.setItem(SESSION_KEY_ENV, matching.id);
            }
            return matching.id;
          }
          // Otherwise fall back to first environment
          if (items.length > 0) {
            const first = items[0];
            if (typeof window !== "undefined") {
              window.sessionStorage.setItem(SESSION_KEY_ENV, first.id);
            }
            return first.id;
          }
          return prev;
        });
      })
      .catch((error: unknown) => {
        if (closed) return;
        setEnvironments([]);
        setEnvironmentsError(
          error instanceof Error ? error.message : "Failed to load environments",
        );
      })
      .finally(() => {
        if (!closed) setEnvironmentsLoading(false);
      });

    return () => {
      closed = true;
    };
  }, []);

  // Changing mode resets environment selection if the current env belongs to the other mode.
  // If the current env doesn't match, auto-select the first env in the new mode.
  const setSelectedMode = useCallback(
    (value: WorkspaceMode) => {
      setSelectedModeState(value);
      if (typeof window === "undefined") return;

      const currentEnv = environments.find((e) => e.id === selectedEnvironmentId) ?? null;
      const envMatchesMode =
        currentEnv && (currentEnv.mode === "live" ? "live" : "paper") === value;

      let nextEnvId: string;
      if (envMatchesMode) {
        nextEnvId = selectedEnvironmentId;
      } else {
        // Auto-select the first environment matching the new mode
        const matching = environments.find(
          (e) => (e.mode === "live" ? "live" : "paper") === value,
        );
        nextEnvId = matching ? matching.id : "";
      }

      setSelectedEnvironmentIdState(nextEnvId);

      const url = new URL(window.location.href);
      url.searchParams.set("mode", value);
      if (nextEnvId) {
        url.searchParams.set("env", nextEnvId);
        window.sessionStorage.setItem(SESSION_KEY_ENV, nextEnvId);
      } else {
        url.searchParams.delete("env");
        url.searchParams.delete("environment_id");
        window.sessionStorage.removeItem(SESSION_KEY_ENV);
      }
      window.sessionStorage.setItem(SESSION_KEY_MODE, value);
      window.history.replaceState({}, "", url.toString());
    },
    [environments, selectedEnvironmentId],
  );

  // Changing environment also syncs the mode
  const setSelectedEnvironmentId = useCallback(
    (value: string) => {
      setSelectedEnvironmentIdState(value);
      if (typeof window === "undefined") return;

      const env = environments.find((e) => e.id === value) ?? null;
      const nextMode: WorkspaceMode =
        env?.mode === "live" ? "live" : env?.mode ? "paper" : selectedModeState;

      setSelectedModeState(nextMode);

      const url = new URL(window.location.href);
      url.searchParams.set("mode", nextMode);
      window.sessionStorage.setItem(SESSION_KEY_MODE, nextMode);

      if (value) {
        url.searchParams.set("env", value);
        // Remove legacy param if present so we don't have duplicates
        url.searchParams.delete("environment_id");
        window.sessionStorage.setItem(SESSION_KEY_ENV, value);
      } else {
        url.searchParams.delete("env");
        url.searchParams.delete("environment_id");
        window.sessionStorage.removeItem(SESSION_KEY_ENV);
      }
      window.history.replaceState({}, "", url.toString());
    },
    [environments, selectedModeState],
  );

  const selectedEnvironment = useMemo(
    () => environments.find((e) => e.id === selectedEnvironmentId) ?? null,
    [environments, selectedEnvironmentId],
  );

  // Derive mode from the selected environment when one is chosen
  const selectedMode: WorkspaceMode = selectedEnvironment
    ? selectedEnvironment.mode === "live"
      ? "live"
      : "paper"
    : selectedModeState;

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      environments,
      environmentsLoading,
      environmentsError,
      selectedMode,
      selectedEnvironmentId,
      selectedEnvironment,
      setSelectedMode,
      setSelectedEnvironmentId,
    }),
    [
      environments,
      environmentsError,
      environmentsLoading,
      selectedEnvironment,
      selectedEnvironmentId,
      selectedMode,
      setSelectedEnvironmentId,
      setSelectedMode,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useOptionalWorkspace();
  if (!ctx) {
    throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  }
  return ctx;
}

export function useOptionalWorkspace(): WorkspaceContextValue | null {
  return useContext(WorkspaceContext);
}
