"use client";

import { useEffect, useState } from "react";

type MarkdownNoteEditorProps = {
  initialMarkdown?: string;
  title?: string;
  helperText?: string;
  placeholder?: string;
  disabled?: boolean;
  onSave: (markdown: string) => Promise<void>;
};

export function MarkdownNoteEditor({
  initialMarkdown = "",
  title,
  helperText,
  placeholder,
  disabled = false,
  onSave,
}: MarkdownNoteEditorProps) {
  const [value, setValue] = useState(initialMarkdown);
  const [saving, setSaving] = useState(false);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);

  useEffect(() => {
    setValue(initialMarkdown);
  }, [initialMarkdown]);

  async function handleSave() {
    if (disabled) {
      return;
    }
    setSaving(true);
    setSavedText(null);
    setErrorText(null);
    try {
      await onSave(value);
      setSavedText("Saved");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-2">
      {title ? <p className="text-sm font-medium text-foreground/85">{title}</p> : null}
      <textarea
        aria-label="Markdown note"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        rows={8}
        placeholder={placeholder}
        disabled={disabled}
        className="w-full rounded-lg border border-border/70 bg-background px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-60"
      />
      <p className="text-xs text-foreground/60">{helperText ?? "Preview ready"}</p>
      <button
        type="button"
        onClick={handleSave}
        disabled={saving || disabled}
        className="rounded-lg border border-border/70 bg-background px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-60"
      >
        {saving ? "Saving..." : "Save"}
      </button>
      {savedText ? <p className="text-sm text-emerald-400">{savedText}</p> : null}
      {errorText ? <p className="text-sm text-rose-400">{errorText}</p> : null}
    </div>
  );
}
