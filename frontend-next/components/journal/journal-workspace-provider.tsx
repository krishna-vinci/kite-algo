"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { fetchJournalEnvironments } from "@/lib/journal/api";
import type { JournalEnvironment } from "@/lib/journal/types";

type JournalWorkspaceContextValue = {
  environments: JournalEnvironment[];
  environmentsLoading: boolean;
  environmentsError: string | null;
  selectedEnvironmentId: string;
  selectedEnvironment: JournalEnvironment | null;
  setSelectedEnvironmentId: (value: string) => void;
};

const SESSION_KEY = "journal.v2.environment_id";

const JournalWorkspaceContext = createContext<JournalWorkspaceContextValue | null>(null);

export function JournalWorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [environments, setEnvironments] = useState<JournalEnvironment[]>([]);
  const [environmentsLoading, setEnvironmentsLoading] = useState(true);
  const [environmentsError, setEnvironmentsError] = useState<string | null>(null);
  const [selectedEnvironmentId, setSelectedEnvironmentIdState] = useState("");

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const fromUrl = new URLSearchParams(window.location.search).get("environment_id") ?? "";
    const fromSession = window.sessionStorage.getItem(SESSION_KEY) ?? "";
    setSelectedEnvironmentIdState(fromUrl || fromSession);
  }, []);

  useEffect(() => {
    let closed = false;
    setEnvironmentsLoading(true);

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

  function setSelectedEnvironmentId(value: string) {
    setSelectedEnvironmentIdState(value);
    if (typeof window === "undefined") {
      return;
    }

    const url = new URL(window.location.href);
    if (value) {
      url.searchParams.set("environment_id", value);
      window.sessionStorage.setItem(SESSION_KEY, value);
    } else {
      url.searchParams.delete("environment_id");
      window.sessionStorage.removeItem(SESSION_KEY);
    }
    window.history.replaceState({}, "", url.toString());
  }

  const selectedEnvironment = useMemo(
    () => environments.find((item) => item.id === selectedEnvironmentId) ?? null,
    [environments, selectedEnvironmentId],
  );

  const value = useMemo(
    () => ({
      environments,
      environmentsLoading,
      environmentsError,
      selectedEnvironmentId,
      selectedEnvironment,
      setSelectedEnvironmentId,
    }),
    [environments, environmentsLoading, environmentsError, selectedEnvironmentId, selectedEnvironment],
  );

  return <JournalWorkspaceContext.Provider value={value}>{children}</JournalWorkspaceContext.Provider>;
}

export function useJournalWorkspace() {
  const context = useContext(JournalWorkspaceContext);
  if (!context) {
    throw new Error("useJournalWorkspace must be used inside JournalWorkspaceProvider");
  }
  return context;
}
