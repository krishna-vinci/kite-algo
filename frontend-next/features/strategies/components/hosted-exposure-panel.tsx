"use client";

/**
 * The strategy's own projected book.
 *
 * This is the attributed strategy book, not the account's net position, and an
 * empty list means "no attributed open position is recorded" - never "flat".
 * A row the platform could not map to a canonical instrument carries its
 * unresolved reason, so unknown exposure is visible instead of dropped.
 */

import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useHostedPositions } from "@/features/strategies/hooks/use-hosted-strategies-queries";
import { hostedErrorMessage } from "@/features/strategies/lib/format";
import { environmentLabel, supportedExecutionModes } from "@/features/strategies/lib/modes";
import type { HostedStrategyOptions } from "@/lib/hosted-strategies/types";

export function HostedExposurePanel({
  strategyId,
  options,
  defaultEnvironment,
}: Readonly<{
  strategyId: string;
  options: HostedStrategyOptions | undefined;
  defaultEnvironment: string;
}>) {
  const [environmentOverride, setEnvironmentOverride] = useState<string | null>(null);
  const selectedEnvironment = environmentOverride ?? defaultEnvironment;
  const positionsQuery = useHostedPositions(strategyId, selectedEnvironment);
  const rows = positionsQuery.data?.positions ?? [];
  const unresolved = rows.filter((row) => row.unresolved_reason);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={selectedEnvironment} onValueChange={setEnvironmentOverride}>
          <SelectTrigger className="w-72 max-w-full" aria-label="Environment">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {supportedExecutionModes(options).map((mode) => (
              <SelectItem key={mode} value={mode}>
                {environmentLabel(mode)}
              </SelectItem>
            ))}
            {!supportedExecutionModes(options).includes(selectedEnvironment) ? (
              <SelectItem value={selectedEnvironment}>
                {environmentLabel(selectedEnvironment)}
              </SelectItem>
            ) : null}
          </SelectContent>
        </Select>
      </div>
      {positionsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load the strategy&apos;s book</AlertTitle>
          <AlertDescription>{hostedErrorMessage(positionsQuery.error)}</AlertDescription>
        </Alert>
      ) : positionsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">Loading the strategy&apos;s book…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="exposure-empty">
          No attributed open position is recorded for this strategy in this environment. That is not a
          flat account: it only means nothing attributed is open here.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Instrument</TableHead>
              <TableHead>Product</TableHead>
              <TableHead className="text-right">Net quantity</TableHead>
              <TableHead>Unresolved</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={`${row.identity_kind}:${row.identity_key}`}>
                <TableCell className="font-mono text-xs">
                  {row.tradingsymbol}
                  <span className="ml-2 text-muted-foreground">{row.exchange}</span>
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">{row.product}</TableCell>
                <TableCell className="text-right text-sm">{row.net_quantity}</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {row.unresolved_reason ?? "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {unresolved.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          {unresolved.length} row{unresolved.length === 1 ? "" : "s"} could not be mapped to a canonical
          instrument, so that exposure is unknown rather than counted as zero.
        </p>
      ) : null}
    </div>
  );
}
