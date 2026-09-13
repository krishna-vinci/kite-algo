"use client";

import { useParams } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { UniverseDetailPage } from "@/features/alerts/components/universe-detail-page";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export default function AlertsUniverseDetailPage() {
  const params = useParams<{ name: string }>();
  const { scope, isLoading } = useAlertsScope();
  const name = typeof params?.name === "string" ? decodeURIComponent(params.name) : "";

  if (isLoading || !name) return <Skeleton className="h-96 w-full rounded-xl" />;
  return <UniverseDetailPage name={name} scope={scope} />;
}
