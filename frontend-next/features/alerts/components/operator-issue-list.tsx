import { AlertCircleIcon, TriangleAlertIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { AlertsIssue } from "@/features/alerts/types";

type OperatorIssueListProps = Readonly<{
  issues: AlertsIssue[];
  /** Renders a compact list without the surrounding card. */
  bare?: boolean;
}>;

/**
 * Validation issues grouped by severity.
 *
 * Errors block activation; warnings do not change validity at all (a rule that
 * can never notify is still a legal document). They are shown as separate
 * groups because "will not fire" and "will not save" call for different
 * operator responses.
 */
export function OperatorIssueList({ issues, bare = false }: OperatorIssueListProps) {
  const errors = issues.filter((issue) => issue.severity === "error");
  const warnings = issues.filter((issue) => issue.severity !== "error");
  if (errors.length === 0 && warnings.length === 0) return null;

  const body = (
    <div className="flex flex-col gap-3">
      {errors.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm text-rose-300">
          {errors.map((issue, index) => (
            <li key={`error-${issue.where}-${issue.code}-${index}`} className="flex items-start gap-2">
              <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                <span className="font-mono text-xs opacity-70">{issue.where}</span> {issue.message}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {warnings.length > 0 ? (
        <ul className="flex flex-col gap-1 text-sm text-amber-300">
          {warnings.map((issue, index) => (
            <li key={`warning-${issue.where}-${issue.code}-${index}`} className="flex items-start gap-2">
              <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                <span className="font-mono text-xs opacity-70">{issue.where}</span> {issue.message}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );

  if (bare) return body;

  return (
    <Alert>
      <AlertTitle>
        {errors.length > 0 ? "This alert will not activate" : "Advisory warnings"}
      </AlertTitle>
      <AlertDescription>{body}</AlertDescription>
    </Alert>
  );
}
