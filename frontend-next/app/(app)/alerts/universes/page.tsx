"use client";

import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { UniversesPage } from "@/features/alerts/components/universes-page";

export default function AlertsUniversesPage() {
  return <AlertsScopeGate>{(scope) => <UniversesPage scope={scope} />}</AlertsScopeGate>;
}
