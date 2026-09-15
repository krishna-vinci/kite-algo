"use client";

import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { OperationsPage } from "@/features/alerts/components/operations-page";

export default function AlertsOperationsPage() {
  return <AlertsScopeGate>{(scope) => <OperationsPage scope={scope} />}</AlertsScopeGate>;
}
