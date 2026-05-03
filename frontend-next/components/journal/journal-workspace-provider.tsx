"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { fetchJournalEnvironments } from "@/lib/journal/api";
import type { JournalEnvironment } from "@/lib/journal/types";

type JournalWorkspaceContextValue = {
  environments: JournalEnvironment[];
  environmentsLoading: boolean;
  environmentsError: string | null;
  selectedMode: "live" | "paper";
  selectedEnvironmentId: string;
  selectedEnvironment: JournalEnvironment | null;
  setSelectedMode: (value: "live" | "paper") => void;
  setSelectedEnvironmentId: (value: string) => void;
};

const SESSION_KEY = "journal.v2.environment_id";
const MODE_SESSION_KEY = "journal.v2.mode";

const JournalWorkspaceContext = createContext<JournalWorkspaceContextValue | null>(null);

function getInitialSelectedEnvironmentId() {
  if (typeof window === "undefined") {
    return "";
  }

  const fromUrl = new URLSearchParams(window.location.search).get("environment_id") ?? "";
  const fromSession = window.sessionStorage.getItem(SESSION_KEY) ?? "";
  return fromUrl || fromSession;
}

export function JournalWorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [environments, setEnvironments] = useState<JournalEnvironment[]>([]);
  const [environmentsLoading, setEnvironmentsLoading] = useState(true);
  const [environmentsError, setEnvironmentsError] = useState<string | null>(null);
  const [selectedModeState, setSelectedModeState] = useState<"live" | "paper">(() => {
    if (typeof window === "undefined") {
      return "live";
    }
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("mode");
    if (fromUrl === "live" || fromUrl === "paper") {
      return fromUrl;
    }
    const fromSession = window.sessionStorage.getItem(MODE_SESSION_KEY);
    return fromSession === "paper" ? "paper" : "live";
  });
  const [selectedEnvironmentId, setSelectedEnvironmentIdState] = useState(getInitialSelectedEnvironmentId);

  useEffect(() => {
    let closed = false;

    fetchJournalEnvironments()
      .then((items) => {
        if (closed) {
          return;
        }
        setEnvironments(items);
        setEnvironmentsError(null);
      })
      .catch((error) => {
        if (closed) {
          return;
        }
        setEnvironments([]);
        setEnvironmentsError(error instanceof Error ? error.message : "Failed to load environments");
      })
      .finally(() => {
        if (!closed) {
          setEnvironmentsLoading(false);
        }
      });

    return () => {
      closed = true;
    };
  }, []);

  const setSelectedMode = useCallback((value: "live" | "paper") => {
    setSelectedModeState(value);
    if (typeof window === "undefined") {
      return;
    }

    const selectedEnvironment = environments.find((item) => item.id === selectedEnvironmentId) ?? null;
    const nextEnvironmentId = selectedEnvironment && (selectedEnvironment.mode === "live" ? "live" : "paper") === value
      ? selectedEnvironmentId
      : "";

    setSelectedEnvironmentIdState(nextEnvironmentId);

    const url = new URL(window.location.href);
    url.searchParams.set("mode", value);
    if (nextEnvironmentId) {
      url.searchParams.set("environment_id", nextEnvironmentId);
      window.sessionStorage.setItem(SESSION_KEY, nextEnvironmentId);
    } else {
      url.searchParams.delete("environment_id");
      window.sessionStorage.removeItem(SESSION_KEY);
    }
    window.sessionStorage.setItem(MODE_SESSION_KEY, value);
    window.history.replaceState({}, "", url.toString());
  }, [environments, selectedEnvironmentId]);

  const setSelectedEnvironmentId = useCallback((value: string) => {
    setSelectedEnvironmentIdState(value);
    if (typeof window === "undefined") {
      return;
    }

    const url = new URL(window.location.href);
    const selectedEnvironment = environments.find((item) => item.id === value) ?? null;
    const nextMode = selectedEnvironment?.mode === "live" ? "live" : selectedEnvironment?.mode ? "paper" : selectedModeState;
    url.searchParams.set("mode", nextMode);
    window.sessionStorage.setItem(MODE_SESSION_KEY, nextMode);
    setSelectedModeState(nextMode);
    if (value) {
      url.searchParams.set("environment_id", value);
      window.sessionStorage.setItem(SESSION_KEY, value);
    } else {
      url.searchParams.delete("environment_id");
      window.sessionStorage.removeItem(SESSION_KEY);
    }
    window.history.replaceState({}, "", url.toString());
  }, [environments, selectedModeState]);

  const selectedEnvironment = useMemo(
    () => environments.find((item) => item.id === selectedEnvironmentId) ?? null,
    [environments, selectedEnvironmentId],
  );
  const selectedMode = selectedEnvironment ? (selectedEnvironment.mode === "live" ? "live" : "paper") : selectedModeState;

  const value = useMemo(
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
    [environments, environmentsError, environmentsLoading, selectedEnvironment, selectedEnvironmentId, selectedMode, setSelectedEnvironmentId, setSelectedMode],
  );

  return <JournalWorkspaceContext.Provider value={value}>{children}</JournalWorkspaceContext.Provider>;
}

export function useJournalWorkspace() {
  const context = useOptionalJournalWorkspace();
  if (!context) {
    throw new Error("useJournalWorkspace must be used inside JournalWorkspaceProvider");
  }
  return context;
}

export function useOptionalJournalWorkspace() {
  return useContext(JournalWorkspaceContext);
}
