"use client";

import { useParams } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { UniverseDetailPage } from "@/features/alerts/components/universe-detail-page";

export default function AlertsUniverseDetailPage() {
  const params = useParams<{ name: string }>();
  const name = typeof params?.name === "string" ? decodeURIComponent(params.name) : "";

  if (!name) return <Skeleton className="h-96 w-full rounded-xl" />;

  return (
    <AlertsScopeGate>
      {(scope) => <UniverseDetailPage name={name} scope={scope} />}
    </AlertsScopeGate>
  );
}
