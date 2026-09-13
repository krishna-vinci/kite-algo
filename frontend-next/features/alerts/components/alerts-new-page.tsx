"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { AlertWizard } from "@/features/alerts/components/alert-wizard";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export function AlertsNewPage() {
  const { scope, isLoading } = useAlertsScope();

  if (isLoading) {
    return <Skeleton className="h-96 w-full rounded-xl" />;
  }

  return <AlertWizard scope={scope} />;
}
