"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { AlertsScopeGate } from "@/features/alerts/components/alerts-scope-gate";
import { QuickScreenerComposer } from "@/features/alerts/components/quick-screener-composer";
import { ScreenerEditor } from "@/features/alerts/components/screener-editor";

export default function NewScreenerPage() {
  const params = useSearchParams();
  const advanced = params.get("mode") === "advanced";

  return (
    <AlertsScopeGate>
      {(scope) =>
        advanced ? (
          <div className="flex flex-col gap-4 pb-8">
            <Link className="text-xs underline text-muted-foreground" href="/alerts/screeners/new">
              Back to the quick form
            </Link>
            <ScreenerEditor scope={scope} />
          </div>
        ) : (
          <QuickScreenerComposer scope={scope} />
        )
      }
    </AlertsScopeGate>
  );
}
