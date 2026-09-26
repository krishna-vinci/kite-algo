"use client";

/**
 * Cross-strategy approvals inbox: every execution request in
 * `awaiting_approval` across the owner's hosted strategies, in one list.
 * Approve/reject reuse the same per-strategy decision routes the strategy
 * detail page uses.
 */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SectionLabel } from "@/components/operator/section-label";
import { hostedKeys } from "@/features/strategies/hooks/keys";
import { usePendingApprovals } from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { formatTimestamp, hostedErrorMessage } from "@/features/strategies/lib/format";
import { approveExecutionRequest, rejectExecutionRequest } from "@/lib/hosted-strategies/api";
import type { PendingApprovalItem } from "@/lib/hosted-strategies/types";

function expiresLabel(value: string | null): string {
  if (!value) return "No expiry";
  const target = new Date(value).getTime();
  if (Number.isNaN(target)) return "Unknown";
  const diffMs = target - Date.now();
  if (diffMs <= 0) return "Expired";
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "< 1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function ApprovalRow({
  item,
  onDecision,
  busy,
}: Readonly<{
  item: PendingApprovalItem;
  onDecision: (item: PendingApprovalItem, decision: "approve" | "reject") => void;
  busy: string | null;
}>) {
  const isBusy = busy === item.request_id;
  return (
    <TableRow>
      <TableCell className="align-top text-sm font-medium">{item.strategy_name}</TableCell>
      <TableCell className="align-top text-sm text-muted-foreground">{item.summary}</TableCell>
      <TableCell className="align-top">
        <Badge variant={item.environment === "live" ? "default" : "secondary"}>{item.environment}</Badge>
      </TableCell>
      <TableCell className="align-top text-xs text-muted-foreground">
        {formatTimestamp(item.created_at)}
      </TableCell>
      <TableCell className="align-top text-xs text-muted-foreground" data-testid={`expires-${item.request_id}`}>
        {expiresLabel(item.expires_at)}
      </TableCell>
      <TableCell className="align-top text-right">
        <div className="flex justify-end gap-2">
          <Button size="sm" disabled={isBusy} onClick={() => onDecision(item, "approve")}>
            Approve
          </Button>
          <Button size="sm" variant="outline" disabled={isBusy} onClick={() => onDecision(item, "reject")}>
            Reject
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

export function ApprovalsInboxPage() {
  const approvalsQuery = usePendingApprovals();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const items = approvalsQuery.data?.items ?? [];

  async function onDecision(item: PendingApprovalItem, decision: "approve" | "reject") {
    setBusy(item.request_id);
    try {
      if (decision === "approve") {
        await approveExecutionRequest(item.strategy_id, item.request_id);
      } else {
        await rejectExecutionRequest(item.strategy_id, item.request_id);
      }
      toast.success(decision === "approve" ? "Approved." : "Rejected.");
      await queryClient.invalidateQueries({ queryKey: hostedKeys.pendingApprovals() });
    } catch (error) {
      toast.error(hostedErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <SectionLabel
        eyebrow="Hosted strategies"
        title="Approvals"
        description="Execution requests awaiting your decision, across every strategy."
      />

      {approvalsQuery.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : approvalsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load approvals</AlertTitle>
          <AlertDescription>{hostedErrorMessage(approvalsQuery.error)}</AlertDescription>
        </Alert>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is waiting for your approval.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead>Summary</TableHead>
              <TableHead>Environment</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <ApprovalRow key={item.request_id} item={item} onDecision={onDecision} busy={busy} />
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
