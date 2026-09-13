"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { OperationsPage } from "@/features/alerts/components/operations-page";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export default function AlertsOperationsPage() {
  const { scope, isLoading } = useAlertsScope();
  if (isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;
  return <OperationsPage scope={scope} />;
}
