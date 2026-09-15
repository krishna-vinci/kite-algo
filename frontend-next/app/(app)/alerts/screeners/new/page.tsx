"use client";

import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";

export default function NewScreenerPage() {
  return <AlertsScopeGate>{(scope) => <ScreenerEditor scope={scope} />}</AlertsScopeGate>;
}
