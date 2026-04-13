"use client";

import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Panel } from "@/components/operator/panel";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  fetchIndexConstituents,
  fetchIndexStatus,
  refreshConstituents,
  refreshLiveMetrics,
  saveBaseline,
  seedDefaultBaseline,
  type BaselineEntry,
  type IndexConstituentRow,
  type IndexRefreshState,
  type IndexSourceList,
} from "@/lib/settings/api";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const INDEX_CONFIGS: {
  sourceList: IndexSourceList;
  label: string;
  shortLabel: string;
  hasBaseline: boolean;
  hasLiveRefresh: boolean;
}[] = [
  { sourceList: "Nifty50", label: "Nifty 50", shortLabel: "N50", hasBaseline: true, hasLiveRefresh: true },
  { sourceList: "NiftyBank", label: "Nifty Bank", shortLabel: "NBNK", hasBaseline: true, hasLiveRefresh: true },
  { sourceList: "Nifty500", label: "Nifty 500", shortLabel: "N500", hasBaseline: false, hasLiveRefresh: false },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return "—";
  }
}

function resolveStatusTone(state: IndexRefreshState | undefined): {
  tone: "positive" | "warning" | "danger" | "neutral";
  label: string;
} {
  if (!state) return { tone: "neutral", label: "unknown" };
  if (state.last_error) return { tone: "danger", label: "error" };
  if (state.needs_review || state.pending_review_count > 0) return { tone: "warning", label: "review needed" };
  if (state.added_symbols.length > 0 || state.removed_symbols.length > 0)
    return { tone: "warning", label: "constituents changed" };
  return { tone: "positive", label: "ready" };
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ActionButton({
  onClick,
  disabled,
  loading,
  children,
  variant = "default",
}: {
  onClick: () => void;
  disabled?: boolean;
  loading?: boolean;
  children: React.ReactNode;
  variant?: "default" | "primary" | "danger";
}) {
  const base =
    "rounded-xl border px-3 py-2 text-[11px] font-medium uppercase tracking-[0.16em] transition-colors disabled:opacity-40 disabled:cursor-not-allowed";
  const variants = {
    default: "border-border/70 bg-background/60 text-foreground/70 hover:border-primary/30 hover:text-foreground",
    primary: "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20",
    danger: "border-rose-400/30 bg-rose-400/10 text-rose-300 hover:bg-rose-400/20",
  };
  return (
    <button type="button" className={`${base} ${variants[variant]}`} onClick={onClick} disabled={disabled || loading}>
      {loading ? "…" : children}
    </button>
  );
}

function SymbolPills({ symbols, tone }: { symbols: string[]; tone: "positive" | "danger" }) {
  if (symbols.length === 0) return null;
  const color = tone === "positive" ? "border-emerald-400/20 text-emerald-300" : "border-rose-400/20 text-rose-300";
  return (
    <div className="flex flex-wrap gap-1.5">
      {symbols.map((s) => (
        <span key={s} className={`rounded-lg border px-2 py-0.5 font-mono text-[10px] ${color}`}>
          {s}
        </span>
      ))}
    </div>
  );
}

function MetricSlot({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/60 p-2.5">
      <p className="text-[9px] uppercase tracking-[0.35em] text-foreground/40">{label}</p>
      <p className="mt-1 font-mono text-xs text-primary">{value}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Index status card
// ---------------------------------------------------------------------------

function IndexStatusCard({
  sourceList,
  label,
  hasBaseline,
  hasLiveRefresh,
  onOpenEditor,
}: {
  sourceList: IndexSourceList;
  label: string;
  hasBaseline: boolean;
  hasLiveRefresh: boolean;
  onOpenEditor: () => void;
}) {
  const queryClient = useQueryClient();

  const statusQuery = useQuery({
    queryKey: ["indexStatus", sourceList],
    queryFn: () => fetchIndexStatus(sourceList),
    staleTime: 60_000,
  });

  const state = statusQuery.data;
  const { tone, label: statusLabel } = resolveStatusTone(state);

  const refreshConst = useMutation({
    mutationFn: () => refreshConstituents([sourceList]),
    onSuccess: () => {
      toast.success(`${label} constituents refreshed`);
      void queryClient.invalidateQueries({ queryKey: ["indexStatus", sourceList] });
      void queryClient.invalidateQueries({ queryKey: ["indexConstituents", sourceList] });
    },
    onError: () => toast.error(`Failed to refresh ${label} constituents`),
  });

  const refreshLive = useMutation({
    mutationFn: () => refreshLiveMetrics(),
    onSuccess: () => {
      toast.success(`Live metrics refreshed`);
      void queryClient.invalidateQueries({ queryKey: ["indexStatus", sourceList] });
      void queryClient.invalidateQueries({ queryKey: ["indexConstituents", sourceList] });
    },
    onError: () => toast.error(`Failed to refresh live metrics`),
  });

  const reseedDefault = useMutation({
    mutationFn: () => seedDefaultBaseline(sourceList as "Nifty50" | "NiftyBank"),
    onSuccess: () => {
      toast.success(`${label} default baseline seeded`);
      void queryClient.invalidateQueries({ queryKey: ["indexStatus", sourceList] });
      void queryClient.invalidateQueries({ queryKey: ["indexConstituents", sourceList] });
    },
    onError: () => toast.error(`Failed to reseed ${label} baseline`),
  });

  const anyLoading = refreshConst.isPending || refreshLive.isPending || reseedDefault.isPending;

  return (
    <div className="rounded-2xl border border-border/60 bg-background/60 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.35em] text-foreground/40">{sourceList}</p>
          <p className="mt-1 text-sm font-semibold tracking-tight">{label}</p>
        </div>
        <StatusBadge tone={tone}>{statusLabel}</StatusBadge>
      </div>

      {statusQuery.isLoading && (
        <p className="mt-3 text-xs text-foreground/40">Loading status…</p>
      )}

      {statusQuery.isError && (
        <p className="mt-3 text-xs text-rose-300">Failed to load status</p>
      )}

      {state && (
        <>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <MetricSlot label="Constituent refresh" value={formatTs(state.last_constituent_refresh_at)} />
            {hasLiveRefresh && (
              <MetricSlot label="Live refresh" value={formatTs(state.last_live_refresh_at)} />
            )}
            {state.pending_review_count > 0 && (
              <MetricSlot label="Pending review" value={`${state.pending_review_count} symbols`} />
            )}
          </div>

          {(state.added_symbols.length > 0 || state.removed_symbols.length > 0) && (
            <div className="mt-3 space-y-2">
              {state.added_symbols.length > 0 && (
                <div>
                  <p className="mb-1 text-[9px] uppercase tracking-[0.35em] text-emerald-300/70">Added</p>
                  <SymbolPills symbols={state.added_symbols} tone="positive" />
                </div>
              )}
              {state.removed_symbols.length > 0 && (
                <div>
                  <p className="mb-1 text-[9px] uppercase tracking-[0.35em] text-rose-300/70">Removed</p>
                  <SymbolPills symbols={state.removed_symbols} tone="danger" />
                </div>
              )}
            </div>
          )}

          {state.last_error && (
            <p className="mt-3 rounded-xl border border-rose-400/20 bg-rose-400/5 px-3 py-2 font-mono text-[10px] text-rose-300">
              {state.last_error}
            </p>
          )}
        </>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <ActionButton onClick={() => refreshConst.mutate()} loading={refreshConst.isPending} disabled={anyLoading}>
          Refresh constituents
        </ActionButton>
        {hasLiveRefresh && (
          <ActionButton onClick={() => refreshLive.mutate()} loading={refreshLive.isPending} disabled={anyLoading}>
            Refresh live
          </ActionButton>
        )}
        {hasBaseline && (
          <>
            <ActionButton
              onClick={() => reseedDefault.mutate()}
              loading={reseedDefault.isPending}
              disabled={anyLoading}
            >
              Reseed default
            </ActionButton>
            <ActionButton onClick={onOpenEditor} variant="primary" disabled={anyLoading}>
              Edit baseline
            </ActionButton>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Baseline editor
// ---------------------------------------------------------------------------

type EditRow = {
  symbol: string;
  company: string;
  sector: string;
  baselineWeight: number | null;
  liveWeight: number | null;
  needsReview: boolean;
  editedWeight: string;
};

function buildEditRows(rows: IndexConstituentRow[]): EditRow[] {
  return rows
    .sort((a, b) => {
      const wa = a.baseline_index_weight ?? a.index_weight ?? 0;
      const wb = b.baseline_index_weight ?? b.index_weight ?? 0;
      return wb - wa;
    })
    .map((r) => ({
      symbol: r.tradingsymbol,
      company: r.company_name ?? "",
      sector: r.sector ?? "",
      baselineWeight: r.baseline_index_weight,
      liveWeight: r.index_weight,
      needsReview: r.needs_weight_review,
      editedWeight: r.baseline_index_weight != null
        ? r.baseline_index_weight.toFixed(4)
        : r.index_weight != null
          ? r.index_weight.toFixed(4)
          : "",
    }));
}

function BaselineEditor({
  sourceList,
  label,
  onClose,
}: {
  sourceList: IndexSourceList;
  label: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const constitQuery = useQuery({
    queryKey: ["indexConstituents", sourceList],
    queryFn: () => fetchIndexConstituents(sourceList),
    staleTime: 60_000,
  });

  const [editRows, setEditRows] = useState<EditRow[] | null>(null);
  const [filterText, setFilterText] = useState("");
  const [showReviewOnly, setShowReviewOnly] = useState(false);

  // Build editable state from server data when it arrives
  const rows = useMemo(() => {
    if (editRows) return editRows;
    if (constitQuery.data) {
      const built = buildEditRows(constitQuery.data);
      // Defer state set so we don't update during render
      queueMicrotask(() => setEditRows(built));
      return built;
    }
    return [];
  }, [constitQuery.data, editRows]);

  const handleWeightChange = useCallback((symbol: string, value: string) => {
    setEditRows((prev) => {
      if (!prev) return prev;
      return prev.map((r) => (r.symbol === symbol ? { ...r, editedWeight: value } : r));
    });
  }, []);

  const filteredRows = useMemo(() => {
    let result = rows;
    if (showReviewOnly) {
      result = result.filter((r) => r.needsReview);
    }
    if (filterText) {
      const lower = filterText.toLowerCase();
      result = result.filter(
        (r) =>
          r.symbol.toLowerCase().includes(lower) ||
          r.company.toLowerCase().includes(lower) ||
          r.sector.toLowerCase().includes(lower),
      );
    }
    return result;
  }, [rows, filterText, showReviewOnly]);

  const weightSum = useMemo(() => {
    return rows.reduce((sum, r) => {
      const val = parseFloat(r.editedWeight);
      return sum + (Number.isFinite(val) ? val : 0);
    }, 0);
  }, [rows]);

  const sumWarning = Math.abs(weightSum - 100) > 0.5;

  const saveMutation = useMutation({
    mutationFn: (entries: BaselineEntry[]) =>
      saveBaseline(sourceList, {
        entries,
        normalized_total: 100,
        normalize_freefloat: true,
      }),
    onSuccess: () => {
      toast.success(`${label} baseline saved`);
      void queryClient.invalidateQueries({ queryKey: ["indexStatus", sourceList] });
      void queryClient.invalidateQueries({ queryKey: ["indexConstituents", sourceList] });
      onClose();
    },
    onError: () => toast.error(`Failed to save ${label} baseline`),
  });

  const handleSave = useCallback(() => {
    if (!rows.length) return;
    const entries: BaselineEntry[] = rows
      .filter((r) => r.editedWeight !== "")
      .map((r) => ({
        symbol: r.symbol,
        weight: parseFloat(r.editedWeight) || 0,
      }));
    saveMutation.mutate(entries);
  }, [rows, saveMutation]);

  return (
    <Panel
      eyebrow={`${sourceList} baseline editor`}
      title={`${label} — edit baseline weights`}
      action={
        <button
          type="button"
          onClick={onClose}
          className="rounded-xl border border-border/70 px-3 py-1.5 text-[10px] uppercase tracking-[0.24em] text-foreground/60 transition-colors hover:text-foreground"
          aria-label="Close editor"
        >
          ✕ Close
        </button>
      }
    >
      <p className="mb-4 max-w-3xl text-xs leading-5 text-foreground/50">
        Baseline weights are normalized to total 100. They anchor live derived metrics (return attribution, points
        contribution) against the index. Edit weights below to reflect the intended index composition. Saving will
        POST entries and normalize free-float market cap proportionally.
      </p>

      {/* Toolbar */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <input
          type="text"
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          placeholder="Filter symbol / company / sector…"
          className="rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-xs text-foreground placeholder:text-foreground/30 focus:border-primary/40 focus:outline-none"
          aria-label="Filter constituents"
        />
        <label className="flex items-center gap-2 text-[10px] uppercase tracking-[0.24em] text-foreground/50 select-none cursor-pointer">
          <input
            type="checkbox"
            checked={showReviewOnly}
            onChange={(e) => setShowReviewOnly(e.target.checked)}
            className="accent-primary"
          />
          Review only
        </label>
        <div className="ml-auto flex items-center gap-3">
          <span
            className={`font-mono text-xs ${sumWarning ? "text-amber-300" : "text-foreground/50"}`}
          >
            Σ {weightSum.toFixed(2)}
          </span>
          {sumWarning && (
            <span className="text-[9px] uppercase tracking-[0.2em] text-amber-300/80">
              ≠ 100 — will be normalized on save
            </span>
          )}
        </div>
      </div>

      {/* Table */}
      {constitQuery.isLoading ? (
        <div className="flex h-32 items-center justify-center text-xs text-foreground/40">Loading constituents…</div>
      ) : constitQuery.isError ? (
        <div className="flex h-32 items-center justify-center text-xs text-rose-300">
          Failed to load constituents
        </div>
      ) : rows.length === 0 ? (
        <div className="flex h-32 items-center justify-center text-xs text-foreground/40">
          No constituent data available
        </div>
      ) : (
        <div className="overflow-auto rounded-2xl border border-border/60">
          <table className="w-full text-left text-[11px]">
            <thead className="sticky top-0 z-10 bg-[var(--panel)] text-[9px] uppercase tracking-[0.2em] text-foreground/40">
              <tr>
                <th className="px-3 py-2 font-medium">#</th>
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 font-medium">Company</th>
                <th className="px-3 py-2 font-medium">Sector</th>
                <th className="px-3 py-2 font-medium text-right">Baseline wt</th>
                <th className="px-3 py-2 font-medium text-right">Live wt</th>
                <th className="px-3 py-2 font-medium text-center">State</th>
                <th className="px-3 py-2 font-medium text-right">Edit weight</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.map((row, index) => (
                <tr
                  key={row.symbol}
                  className={`border-t border-border/40 ${row.needsReview ? "bg-amber-400/[0.03]" : ""}`}
                >
                  <td className="px-3 py-2 text-foreground/30">{index + 1}</td>
                  <td className="px-3 py-2 font-medium text-foreground/90">{row.symbol}</td>
                  <td className="px-3 py-2 text-foreground/50 max-w-[160px] truncate" title={row.company}>
                    {row.company}
                  </td>
                  <td className="px-3 py-2 text-foreground/50">{row.sector}</td>
                  <td className="px-3 py-2 text-right font-mono text-primary/80">
                    {row.baselineWeight != null ? row.baselineWeight.toFixed(2) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-foreground/60">
                    {row.liveWeight != null ? row.liveWeight.toFixed(2) : "—"}
                  </td>
                  <td className="px-3 py-2 text-center">
                    {row.needsReview ? (
                      <StatusBadge tone="warning">review</StatusBadge>
                    ) : (
                      <StatusBadge tone="positive">ok</StatusBadge>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <input
                      type="text"
                      inputMode="decimal"
                      value={row.editedWeight}
                      onChange={(e) => handleWeightChange(row.symbol, e.target.value)}
                      className="w-20 rounded-lg border border-border/50 bg-background/40 px-2 py-1 text-right font-mono text-xs text-primary focus:border-primary/50 focus:outline-none"
                      aria-label={`Weight for ${row.symbol}`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Save bar */}
      <div className="mt-4 flex items-center justify-between">
        <p className="text-[10px] text-foreground/40">
          {filteredRows.length} of {rows.length} rows shown
        </p>
        <div className="flex items-center gap-3">
          <ActionButton onClick={onClose} disabled={saveMutation.isPending}>
            Cancel
          </ActionButton>
          <ActionButton
            onClick={handleSave}
            variant="primary"
            loading={saveMutation.isPending}
            disabled={rows.length === 0}
          >
            Save baseline
          </ActionButton>
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Nifty500 read-only panel
// ---------------------------------------------------------------------------

function Nifty500MembershipPanel() {
  const constitQuery = useQuery({
    queryKey: ["indexConstituents", "Nifty500"],
    queryFn: () => fetchIndexConstituents("Nifty500"),
    staleTime: 120_000,
    enabled: false, // don't auto-fetch; fetch on demand
  });

  const [expanded, setExpanded] = useState(false);

  const handleExpand = () => {
    setExpanded((prev) => !prev);
    if (!constitQuery.data) {
      void constitQuery.refetch();
    }
  };

  const sectors = useMemo(() => {
    if (!constitQuery.data) return {};
    const map: Record<string, number> = {};
    for (const row of constitQuery.data) {
      const sector = row.sector ?? "Other";
      map[sector] = (map[sector] ?? 0) + 1;
    }
    return Object.fromEntries(Object.entries(map).sort((a, b) => b[1] - a[1]));
  }, [constitQuery.data]);

  return (
    <div className="space-y-2">
      <ActionButton onClick={handleExpand}>
        {expanded ? "Collapse membership" : "View membership"}
      </ActionButton>

      {expanded && constitQuery.isLoading && (
        <p className="text-xs text-foreground/40 pl-1">Loading…</p>
      )}

      {expanded && constitQuery.isError && (
        <p className="text-xs text-rose-300 pl-1">Failed to load membership</p>
      )}

      {expanded && constitQuery.data && (
        <div className="rounded-xl border border-border/60 bg-background/40 p-3">
          <p className="mb-2 text-[9px] uppercase tracking-[0.35em] text-foreground/40">
            {constitQuery.data.length} members across {Object.keys(sectors).length} sectors
          </p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(sectors).map(([sector, count]) => (
              <span
                key={sector}
                className="rounded-lg border border-border/50 px-2 py-0.5 text-[10px] text-foreground/60"
              >
                {sector} <span className="text-primary/70">{count}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel export
// ---------------------------------------------------------------------------

export function IndexBaselinesPanel() {
  const [editorTarget, setEditorTarget] = useState<{ sourceList: IndexSourceList; label: string } | null>(null);

  if (editorTarget) {
    return (
      <BaselineEditor
        sourceList={editorTarget.sourceList}
        label={editorTarget.label}
        onClose={() => setEditorTarget(null)}
      />
    );
  }

  return (
    <Panel
      id="index-baselines"
      eyebrow="indices"
      title="Index baselines"
      action={<StatusBadge tone="neutral">operator</StatusBadge>}
    >
      <p className="mb-4 max-w-3xl text-xs leading-5 text-foreground/50">
        Nifty 50 and Nifty Bank use normalized baseline weights (total = 100) for computing live derived
        metrics like return attribution and points contribution. Monthly constituent refreshes update
        membership; baseline weights should be reviewed after any change. Nifty 500 is membership-only.
      </p>

      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {INDEX_CONFIGS.map((config) => (
          <div key={config.sourceList}>
            <IndexStatusCard
              sourceList={config.sourceList}
              label={config.label}
              hasBaseline={config.hasBaseline}
              hasLiveRefresh={config.hasLiveRefresh}
              onOpenEditor={() => setEditorTarget({ sourceList: config.sourceList, label: config.label })}
            />
            {config.sourceList === "Nifty500" && (
              <div className="mt-3 pl-1">
                <Nifty500MembershipPanel />
              </div>
            )}
          </div>
        ))}
      </div>
    </Panel>
  );
}
