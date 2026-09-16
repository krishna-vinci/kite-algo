"use client";

import { useParams } from "next/navigation";

import { HostedStrategyDetailPage } from "@/features/strategies/components/hosted-strategy-detail-page";

export default function StrategyDetailRoute() {
  const params = useParams<{ strategyId: string }>();
  return <HostedStrategyDetailPage strategyId={params.strategyId} />;
}
