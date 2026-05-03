import { KpiCard } from "@/components/operator/kpi-card";
import type { JournalEnvironment, JournalV2AnalyticsMetrics } from "@/lib/journal/types";

function formatAmount(value: number | string | null | undefined) {
  const amount = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(amount)) return "—";
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(amount);
}

function formatPercent(value: number | string | null | undefined) {
  const amount = typeof value === "number" ? value : Number(value ?? null);
  if (!Number.isFinite(amount)) return "—";
  return `${amount.toFixed(1)}%`;
}

export function JournalKpiGrid({
  environment,
  metrics,
  unresolvedCount,
}: Readonly<{
  environment: JournalEnvironment | null;
  metrics: JournalV2AnalyticsMetrics | null;
  unresolvedCount: number;
}>) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
      <KpiCard
        label="Environment"
        value={environment ? environment.display_name || environment.account_scope : "—"}
        note={environment ? `${environment.mode} · epoch ${environment.environment_epoch}` : "Select an environment"}
        className="[&_p:nth-child(2)]:text-lg"
      />
      <KpiCard label="Closed episodes" value={String(metrics?.closed_episode_count ?? 0)} note="Selected environment only" />
      <KpiCard label="Net P&L" value={formatAmount(metrics?.net_pnl)} note="No paper/live mixing" />
      <KpiCard label="Win rate" value={formatPercent(metrics?.win_rate)} note="Closed positive episodes" />
      <KpiCard label="Unresolved" value={String(unresolvedCount)} note="Identity or mapping queue" />
    </div>
  );
}
