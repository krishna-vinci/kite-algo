"use client";

/**
 * New alert: the one authoring page.
 *
 * There is no quick-versus-advanced split any more — this is the same editor the
 * edit route uses, so an alert created here can be edited without switching to a
 * different product, and everything uncommon (extra conditions, groups, universe
 * targeting, sequences, raw YAML/JSON) stays reachable on the same page.
 */

import { Skeleton } from "@/components/ui/skeleton";
import { UnifiedAlertEditor } from "@/features/alerts/components/unified-alert-editor";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";
import { AlertsMarketStreamProvider } from "@/features/alerts/hooks/use-market-stream";
import { emptyDraft } from "@/features/alerts/lib/authoring";
import { applyFrequency } from "@/features/alerts/lib/plain-language";

/**
 * A new alert starts on the common case: a live-price crossing with no target
 * entered yet. The value is deliberately empty rather than 0, so the save area
 * asks for it instead of accepting a meaningless rule.
 */
function newAlertDraft() {
  const base = emptyDraft();
  return {
    ...base,
    clock: "ltp",
    // The calm default for a first alert: tell me once, then stay quiet.
    alert: applyFrequency(base.alert, "once"),
    conditions: [
      {
        left: { kind: "field" as const, name: "ltp" },
        op: "crosses_above",
        right: { kind: "constant" as const, value: 0 },
      },
    ],
  };
}

export function AlertsNewPage() {
  const { scope, isLoading } = useAlertsScope();

  if (isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  return (
    <AlertsMarketStreamProvider scope={scope}>
      <UnifiedAlertEditor scope={scope} mode={{ kind: "create" }} initialDraft={newAlertDraft()} />
    </AlertsMarketStreamProvider>
  );
}
