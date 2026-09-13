"use client";

import { useParams } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { CanvasEditor } from "@/features/alerts/components/canvas-editor";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";

export default function AlertCanvasPage() {
  const params = useParams<{ workflowId: string }>();
  const { scope, isLoading } = useAlertsScope();
  const workflowId = typeof params?.workflowId === "string" ? params.workflowId : "";

  if (isLoading || !workflowId) return <Skeleton className="h-96 w-full rounded-xl" />;
  return <CanvasEditor workflowId={workflowId} scope={scope} />;
}
