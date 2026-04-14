"use client";

import { useEffect, useState, useCallback } from "react";
import type { AnalysisPeriod, JournalRule } from "@/lib/journal/types";
import { fetchRules } from "@/lib/journal/api";
import { JournalNav } from "@/components/journal/journal-nav";
import { JournalHeader } from "@/components/journal/journal-header";
import { RulesPanel } from "@/components/journal/rules-panel";
import { RuleEditorDialog } from "@/components/journal/rule-editor-dialog";

export default function JournalRulesPage() {
  const [period, setPeriod] = useState<AnalysisPeriod>("month");

  const [rules, setRules] = useState<JournalRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingRule, setEditingRule] = useState<JournalRule | null>(null);
  const [dialogMode, setDialogMode] = useState<"create" | "edit" | null>(null);

  const loadRules = useCallback(() => {
    setLoading(true);
    setError(null);

    fetchRules()
      .then((d) => { setRules(d); })
      .catch((e) => { setError(e instanceof Error ? e.message : "Failed to load rules"); })
      .finally(() => { setLoading(false); });
  }, []);

  useEffect(() => {
    let disposed = false;
    fetchRules()
      .then((d) => {
        if (!disposed) {
          setRules(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (!disposed) {
          setError(e instanceof Error ? e.message : "Failed to load rules");
        }
      })
      .finally(() => {
        if (!disposed) {
          setLoading(false);
        }
      });
    return () => {
      disposed = true;
    };
  }, []);

  function handleAdd() {
    setEditingRule(null);
    setDialogMode("create");
  }

  function handleEdit(rule: JournalRule) {
    setEditingRule(rule);
    setDialogMode("edit");
  }

  function handleCloseDialog() {
    setEditingRule(null);
    setDialogMode(null);
  }

  return (
    <div className="space-y-4 pb-4">
      <JournalHeader period={period} onPeriodChange={setPeriod} showPeriodSelector={false} />
      <JournalNav />

      <RulesPanel
        rules={rules}
        loading={loading}
        error={error}
        onEdit={handleEdit}
        onAdd={handleAdd}
      />

      {dialogMode && (
        <RuleEditorDialog
          rule={editingRule}
          mode={dialogMode}
          onClose={handleCloseDialog}
          onSaved={loadRules}
        />
      )}
    </div>
  );
}
