"use client";

import { useState } from "react";
import type { JournalRule, RuleState } from "@/lib/journal/types";
import { createRule, updateRule } from "@/lib/journal/api";

type RuleEditorDialogProps = {
  rule: JournalRule | null;
  mode: "create" | "edit";
  onClose: () => void;
  onSaved: () => void;
};

const RULE_STATES: RuleState[] = ["active", "reinforced", "decaying", "retired"];
const CATEGORIES = ["entry", "exit", "risk", "position-sizing", "process", "general"];

export function RuleEditorDialog({ rule, mode, onClose, onSaved }: RuleEditorDialogProps) {
  const [title, setTitle] = useState(rule?.title ?? "");
  const [description, setDescription] = useState(rule?.description ?? "");
  const [category, setCategory] = useState(rule?.category ?? "general");
  const [state, setState] = useState<RuleState>(rule?.state ?? "active");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;

    setSaving(true);
    setError(null);

    try {
      if (mode === "create") {
        await createRule({ title: title.trim(), description: description.trim(), category });
      } else if (rule) {
        await updateRule(rule.id, {
          title: title.trim(),
          description: description.trim(),
          category,
          state,
        });
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save rule");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-label="Rule editor">
      <button
        type="button"
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close dialog"
      />
      <div className="relative w-full max-w-lg rounded-[1.5rem] border border-border/70 bg-[var(--panel,#0c0d12)] p-6">
        <h2 className="text-base font-semibold tracking-tight">
          {mode === "create" ? "New rule" : "Edit rule"}
        </h2>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div>
            <label htmlFor="rule-title" className="block text-[10px] uppercase tracking-[0.35em] text-foreground/40">
              Title
            </label>
            <input
              id="rule-title"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Never average down on losing positions"
              required
              className="mt-1 w-full rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-foreground/90 placeholder:text-foreground/30 focus:border-primary/40 focus:outline-none"
            />
          </div>

          <div>
            <label htmlFor="rule-description" className="block text-[10px] uppercase tracking-[0.35em] text-foreground/40">
              Description
            </label>
            <textarea
              id="rule-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Explain the rule rationale and conditions..."
              rows={3}
              className="mt-1 w-full rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-foreground/90 placeholder:text-foreground/30 focus:border-primary/40 focus:outline-none"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="rule-category" className="block text-[10px] uppercase tracking-[0.35em] text-foreground/40">
                Category
              </label>
              <select
                id="rule-category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="mt-1 w-full rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-foreground/90 focus:border-primary/40 focus:outline-none"
              >
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>

            {mode === "edit" && (
              <div>
                <label htmlFor="rule-state" className="block text-[10px] uppercase tracking-[0.35em] text-foreground/40">
                  State
                </label>
                <select
                  id="rule-state"
                  value={state}
                  onChange={(e) => setState(e.target.value as RuleState)}
                  className="mt-1 w-full rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-foreground/90 focus:border-primary/40 focus:outline-none"
                >
                  {RULE_STATES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          {error && <p className="text-sm text-rose-300">{error}</p>}

          <div className="flex gap-2 pt-2">
            <button
              type="submit"
              disabled={saving || !title.trim()}
              className="flex-1 rounded-xl border border-primary/40 bg-primary/10 px-4 py-2 text-xs font-medium uppercase tracking-[0.2em] text-primary transition-colors hover:bg-primary/20 disabled:opacity-50"
            >
              {saving ? "Saving..." : mode === "create" ? "Create rule" : "Update rule"}
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="rounded-xl border border-border/70 bg-background/60 px-4 py-2 text-xs font-medium uppercase tracking-[0.2em] text-foreground/60 transition-colors hover:text-foreground disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
