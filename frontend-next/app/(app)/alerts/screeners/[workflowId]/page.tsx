"use client";

import { useParams } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { ScreenerPage } from "@/features/alerts/components/screener-page";

export default function AlertsScreenerPage() {
  const params = useParams<{ workflowId: string }>();
  const workflowId = typeof params?.workflowId === "string" ? params.workflowId : "";

  if (!workflowId) return <Skeleton className="h-96 w-full rounded-xl" />;

  return (
    <AlertsScopeGate>
      {(scope) => <ScreenerPage workflowId={workflowId} scope={scope} />}
    </AlertsScopeGate>
  );
}
