import { AlertTriangleIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/operator/status-badge";
import {
  LIFECYCLE_TONE,
  WARNING_TONE,
  deriveFreshness,
  deriveLifecycle,
  worstWarningSeverity,
} from "@/features/alerts/lib/status";
import type { AlertsFreshness, AlertsWorkflowSummary } from "@/features/alerts/types";

export function LifecycleBadge({ workflow }: Readonly<{ workflow: AlertsWorkflowSummary }>) {
  const lifecycle = deriveLifecycle(workflow);
  return <StatusBadge tone={LIFECYCLE_TONE[lifecycle]}>{lifecycle}</StatusBadge>;
}

/**
 * Freshness is rendered as its own badge next to lifecycle, never merged with
 * it: "active" and "receiving fresh data" are different questions, and an
 * active-but-silent alert must be visibly distinguishable from a working one.
 */
export function FreshnessBadge({ freshness }: Readonly<{ freshness: AlertsFreshness }>) {
  const view = deriveFreshness(freshness);
  return <StatusBadge tone={view.tone}>{view.label}</StatusBadge>;
}

export function WarningBadge({ workflow }: Readonly<{ workflow: AlertsWorkflowSummary }>) {
  const severity = worstWarningSeverity(workflow.warnings);
  if (severity === null) return null;
  const label = severity === "error" ? "will not fire" : "warning";
  return (
    <StatusBadge tone={WARNING_TONE[severity]}>
      <span className="inline-flex items-center gap-1">
        <AlertTriangleIcon className="size-3" aria-hidden />
        {label}
      </span>
    </StatusBadge>
  );
}

export function KindBadge({ kind }: Readonly<{ kind: AlertsWorkflowSummary["kind"] }>) {
  return (
    <Badge variant={kind === "screener" ? "secondary" : "outline"}>
      {kind === "screener" ? "screener" : "alert"}
    </Badge>
  );
}
