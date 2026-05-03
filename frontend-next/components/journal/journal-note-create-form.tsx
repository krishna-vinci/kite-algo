"use client";

import { useState } from "react";

import { createJournalNote } from "@/lib/journal/api";

export function JournalNoteCreateForm({
  environmentId,
  onCreated,
}: Readonly<{
  environmentId: string;
  onCreated: () => Promise<void> | void;
}>) {
  const [title, setTitle] = useState("Review note");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const disabled = !environmentId || saving || body.trim().length === 0;

  async function handleSubmit() {
    if (disabled) return;
    setSaving(true);
    setError(null);

    try {
      await createJournalNote({
        environment_id: environmentId,
        subject_type: "environment",
        subject_id: environmentId,
        note_type: "review",
        title: title.trim() || "Review note",
        body_markdown: body,
        tags: tags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      });
      setBody("");
      await onCreated();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to create note");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-sm font-semibold text-foreground">Create environment note</p>
      <input
        aria-label="Note title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm"
      />
      <textarea
        aria-label="Markdown note"
        value={body}
        onChange={(event) => setBody(event.target.value)}
        rows={7}
        placeholder="Write what happened, what to review, or what to fix…"
        className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm"
      />
      <input
        aria-label="Note tags"
        value={tags}
        onChange={(event) => setTags(event.target.value)}
        placeholder="tags, comma-separated"
        className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm"
      />
      <button
        type="button"
        onClick={handleSubmit}
        disabled={disabled}
        className="rounded-lg border border-primary/40 bg-primary/10 px-3 py-1.5 text-sm text-primary disabled:cursor-not-allowed disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save note"}
      </button>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
    </div>
  );
}
