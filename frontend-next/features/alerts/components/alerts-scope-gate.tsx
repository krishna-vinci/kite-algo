"use client";

import type { ReactNode } from "react";
import { AlertCircleIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { useAlertsScope } from "@/features/alerts/hooks/use-alerts-queries";
import { alertsErrorMessage } from "@/features/alerts/lib/errors";

/**
 * Resolve the operator scope for a page and render the honest states that
 * surround it.
 *
 * `useAlertsScope` is the only source of the scope, and every alerts page needs
 * it before it can query anything. Handling loading and failure in one place
 * means a failed scopes call is never mistaken for "this scope is empty": the
 * child is only rendered once a scope is known.
 */
export function AlertsScopeGate({
  children,
}: Readonly<{ children: (scope: string | null) => ReactNode }>) {
  const { scope, isLoading, isError, error } = useAlertsScope();

  if (isLoading) return <Skeleton className="h-96 w-full rounded-xl" />;

  if (isError) {
    return (
      <Alert variant="destructive">
        <AlertCircleIcon />
        <AlertTitle>Could not load authorized scopes</AlertTitle>
        <AlertDescription>
          {alertsErrorMessage(error, "The scopes request failed.")}
        </AlertDescription>
      </Alert>
    );
  }

  return <>{children(scope)}</>;
}
