"use client";

import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import type { JournalRule, RuleState } from "@/lib/journal/types";

type RulesPanelProps = {
  rules: JournalRule[];
  loading: boolean;
  error: string | null;
  onEdit: (rule: JournalRule) => void;
  onAdd: () => void;
};

function stateTone(state: RuleState): "positive" | "warning" | "danger" | "neutral" {
  if (state === "active" || state === "reinforced") return "positive";
  if (state === "decaying") return "warning";
  if (state === "retired") return "neutral";
  return "neutral";
}

export function RulesPanel({ rules, loading, error, onEdit, onAdd }: RulesPanelProps) {
  return (
    <Panel
      eyebrow="rules"
      title="Trading rules"
      action={
        <button
          type="button"
          onClick={onAdd}
          className="rounded-full border border-dashed border-border/70 px-3 py-1.5 text-[10px] uppercase tracking-[0.24em] text-foreground/50 transition-colors hover:border-primary/40 hover:text-foreground"
        >
          + add rule
        </button>
      }
    >
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-xl bg-background/40" />
          ))}
        </div>
      )}

      {error && <p className="text-sm text-rose-300">Failed to load rules.</p>}

      {!loading && !error && rules.length === 0 && (
        <p className="text-sm text-foreground/50">No rules defined yet. Add your first trading rule.</p>
      )}

      {!loading && !error && rules.length > 0 && (
        <div className="space-y-2">
          {rules.map((rule) => (
            <button
              key={rule.id}
              type="button"
              onClick={() => onEdit(rule)}
              className="flex w-full items-start justify-between gap-3 rounded-xl border border-border/60 bg-background/60 px-4 py-3 text-left transition-colors hover:border-primary/25"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-foreground/90">{rule.title}</p>
                  <span className="text-[10px] uppercase tracking-[0.2em] text-foreground/30">{rule.category}</span>
                </div>
                <p className="mt-1 text-xs leading-5 text-foreground/50">{rule.description}</p>
                {rule.adherence_rate != null && (
                  <p className="mt-1 text-[10px] text-foreground/40">
                    Adherence: {(rule.adherence_rate * 100).toFixed(0)}% ({rule.total_checks} checks)
                  </p>
                )}
              </div>
              <StatusBadge tone={stateTone(rule.state)}>{rule.state}</StatusBadge>
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}
