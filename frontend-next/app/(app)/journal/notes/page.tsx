"use client";

import { useCallback, useEffect, useState } from "react";

import { JournalNoteCreateForm } from "@/components/journal/journal-note-create-form";
import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { Panel } from "@/components/operator/panel";
import { fetchJournalNotes } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalNote } from "@/lib/journal/types";

type NotesState = {
  environmentId: string;
  items: JournalNote[];
  loading: boolean;
  error: string | null;
};

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export default function JournalNotesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
  const [notesState, setNotesState] = useState<NotesState>({ environmentId: "", items: [], loading: false, error: null });

  const loadNotes = useCallback(async (environmentId: string) => {
    try {
      const items = await fetchJournalNotes({ environment_id: environmentId, limit: 50 });
      setNotesState({ environmentId, items, loading: false, error: null });
    } catch (error) {
      setNotesState({
        environmentId,
        items: [],
        loading: false,
        error: error instanceof Error ? error.message : "Failed to load notes",
      });
    }
  }, []);

  useEffect(() => {
    if (!selectedEnvironmentId) {
      return;
    }

    let closed = false;
    fetchJournalNotes({ environment_id: selectedEnvironmentId, limit: 50 })
      .then((items) => {
        if (!closed) {
          setNotesState({ environmentId: selectedEnvironmentId, items, loading: false, error: null });
        }
      })
      .catch((error) => {
        if (!closed) {
          setNotesState({
            environmentId: selectedEnvironmentId,
            items: [],
            loading: false,
            error: error instanceof Error ? error.message : "Failed to load notes",
          });
        }
      });
    return () => {
      closed = true;
    };
  }, [selectedEnvironmentId]);

  const showingSelectedEnvironment = notesState.environmentId === selectedEnvironmentId;
  const notes = showingSelectedEnvironment ? notesState.items : [];
  const notesLoading = Boolean(selectedEnvironmentId) && (!showingSelectedEnvironment || notesState.loading);
  const notesError = showingSelectedEnvironment ? notesState.error : null;

  return (
    <div className="space-y-5 pb-5">
      <JournalWorkspaceHeader period={period} setPeriod={setPeriod} />

      <Panel title="Notes archive" className="p-4 md:p-5">
        {selectedEnvironment ? (
          <p className="mb-3 text-xs text-foreground/60">
            {selectedEnvironment.display_name || selectedEnvironment.account_scope} · {selectedEnvironment.mode}
          </p>
        ) : null}
        {!selectedEnvironmentId ? <p className="text-sm text-foreground/60">Select an environment to load notes.</p> : null}
        {selectedEnvironmentId && notesLoading ? <p className="text-sm text-foreground/60">Loading notes archive…</p> : null}
        {selectedEnvironmentId && notesError ? <p className="text-sm text-destructive">{notesError}</p> : null}
        {selectedEnvironmentId && !notesLoading && !notesError && notes.length === 0 ? (
          <p className="text-sm text-foreground/60">No notes found for this environment yet.</p>
        ) : null}
        {notes.length > 0 ? (
          <ul className="space-y-2">
            {notes.map((note) => (
              <li key={note.id} className="rounded-lg border border-border/60 p-3 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <p className="font-medium">{note.title || "Untitled note"}</p>
                  <span className="text-xs text-foreground/60">{note.note_type}</span>
                </div>
                <p className="mt-1 text-xs text-foreground/60">
                  Subject: {note.subject_type}:{note.subject_id}
                </p>
                <p className="mt-1 text-xs text-foreground/60">Updated: {formatDateTime(note.updated_at)}</p>
              </li>
            ))}
          </ul>
        ) : null}
      </Panel>

      <Panel title="Create note" className="p-4 md:p-5">
        {!selectedEnvironmentId ? <p className="text-sm text-foreground/60">Select an environment before creating notes.</p> : null}
        {selectedEnvironmentId ? (
          <JournalNoteCreateForm environmentId={selectedEnvironmentId} onCreated={() => loadNotes(selectedEnvironmentId)} />
        ) : null}
      </Panel>
    </div>
  );
}
