"use client";

/**
 * New alert: the common path first, the full editor on request.
 *
 * `?mode=advanced` keeps a direct link to the step editor (and to anything only
 * it can express), while the default is the single-screen composer that covers
 * "alert me when [instrument] [condition] [value], via [channel]".
 */

import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import { AlertWizard } from "@/features/alerts/components/alert-wizard";
import { QuickAlertComposer } from "@/features/alerts/components/quick-alert-composer";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export function AlertsNewPage({ mode }: { mode?: string | null }) {
  const { scope, isLoading } = useAlertsScope();

  if (isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  if (mode === "advanced") {
    return (
      <div className="flex flex-col gap-4 pb-8">
        <Link className="text-xs underline text-muted-foreground" href="/alerts/new">
          Back to the quick form
        </Link>
        <AlertWizard scope={scope} />
      </div>
    );
  }

  return <QuickAlertComposer scope={scope} />;
}
