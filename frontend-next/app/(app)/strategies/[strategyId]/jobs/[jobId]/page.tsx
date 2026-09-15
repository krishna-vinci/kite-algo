"use client";

import { useParams } from "next/navigation";

import { HostedJobDetailPage } from "@/features/strategies/components/hosted-job-detail-page";

export default function StrategyJobDetailRoute() {
  const params = useParams<{ strategyId: string; jobId: string }>();
  return <HostedJobDetailPage strategyId={params.strategyId} jobId={params.jobId} />;
}
