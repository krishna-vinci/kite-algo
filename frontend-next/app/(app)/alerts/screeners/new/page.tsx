"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export default function NewScreenerPage() {
  const { scope, isLoading } = useAlertsScope();
  if (isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;
  return <ScreenerEditor scope={scope} />;
}
