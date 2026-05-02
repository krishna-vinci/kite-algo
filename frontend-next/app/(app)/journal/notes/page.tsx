"use client";

import { useEffect, useState } from "react";

import { JournalWorkspaceHeader } from "@/components/journal/journal-workspace-header";
import { useJournalWorkspace } from "@/components/journal/journal-workspace-provider";
import { MarkdownNoteEditor } from "@/components/journal/markdown-note-editor";
import { Panel } from "@/components/operator/panel";
import { fetchJournalNotes } from "@/lib/journal/api";
import type { AnalysisPeriod, JournalNote } from "@/lib/journal/types";

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export default function JournalNotesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");
  const { selectedEnvironmentId, selectedEnvironment } = useJournalWorkspace();
  const [notes, setNotes] = useState<JournalNote[]>([]);
  const [notesLoading, setNotesLoading] = useState(false);
  const [notesError, setNotesError] = useState<string | null>(null);

  useEffect(() => {
    let closed = false;
    if (!selectedEnvironmentId) {
      setNotes([]);
      setNotesLoading(false);
      setNotesError(null);
      return () => {
        closed = true;
      };
    }
    setNotesLoading(true);
    fetchJournalNotes({ environment_id: selectedEnvironmentId, limit: 50 })
      .then((items) => {
        if (!closed) {
          setNotes(items);
          setNotesError(null);
        }
      })
      .catch((error) => {
        if (!closed) {
          setNotes([]);
          setNotesError(error instanceof Error ? error.message : "Failed to load notes");
        }
      })
      .finally(() => {
        if (!closed) {
          setNotesLoading(false);
        }
      });
    return () => {
      closed = true;
    };
  }, [selectedEnvironmentId]);

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

      <Panel title="Quick note capture" className="p-4 md:p-5">
        <p className="mb-3 text-sm text-foreground/65">
          Capture fast context while reviewing episodes. This editor is environment-scoped and suitable for short operator notes.
        </p>
        <MarkdownNoteEditor
          title="Scratchpad"
          helperText={selectedEnvironmentId ? "Draft locally, then copy into episode/subject notes as needed." : "Select an environment before capturing notes."}
          placeholder="Write a quick review thought, TODO, or follow-up cue..."
          disabled={!selectedEnvironmentId}
          onSave={async () => undefined}
        />
      </Panel>
    </div>
  );
}
