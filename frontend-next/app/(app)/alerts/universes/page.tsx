"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { UniversesPage } from "@/features/alerts/components/universes-page";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export default function AlertsUniversesPage() {
  const { scope, isLoading } = useAlertsScope();
  if (isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;
  return <UniversesPage scope={scope} />;
}
