import * as React from "react";

import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableRow,
} from "@/components/ui/table";
import { Separator } from "@/components/ui/separator";
import { MetricValue } from "@/components/shared/metric-value";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type CostBreakdownValues = {
  brokerage?: number | null;
  exchange_txn_charge?: number | null;
  stt?: number | null;
  stamp_duty?: number | null;
  sebi_charge?: number | null;
  gst?: number | null;
  total_taxes?: number | null;
  total_charges?: number | null;
};

type CostBreakdownTableProps = {
  values: CostBreakdownValues;
  className?: string;
  /** Currency formatter — defaults to Indian locale 2 dp */
  formatter?: (v: number) => string;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const DEFAULT_FORMATTER = (v: number) =>
  v.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

type RowDef = {
  key: keyof CostBreakdownValues;
  label: string;
};

const COMPONENT_ROWS: RowDef[] = [
  { key: "brokerage", label: "Brokerage" },
  { key: "exchange_txn_charge", label: "Exchange Txn" },
  { key: "stt", label: "STT" },
  { key: "stamp_duty", label: "Stamp Duty" },
  { key: "sebi_charge", label: "SEBI Charge" },
  { key: "gst", label: "GST" },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * CostBreakdownTable — dense read-only table of trading cost components.
 * Uses shadcn Table primitives; no div-based layout.
 */
export function CostBreakdownTable({
  values,
  className,
  formatter = DEFAULT_FORMATTER,
}: CostBreakdownTableProps) {
  const fmt = (v: number | null | undefined) =>
    v != null ? formatter(v) : null;

  return (
    <div className={cn("w-full", className)}>
      <Table>
        <TableBody>
          {COMPONENT_ROWS.map((row) => (
            <TableRow
              key={row.key}
              className="border-b-0 hover:bg-transparent"
            >
              <TableCell className="py-0.5 pl-0 pr-2 text-xs text-muted-foreground">
                {row.label}
              </TableCell>
              <TableCell className="py-0.5 pr-0 text-right text-xs">
                <MetricValue value={fmt(values[row.key])} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>

        <TableFooter className="border-t border-border bg-transparent">
          <TableRow className="border-b-0 hover:bg-transparent">
            <TableCell className="py-1 pl-0 pr-2 text-xs font-medium text-foreground">
              Total Taxes
            </TableCell>
            <TableCell className="py-1 pr-0 text-right text-xs font-medium">
              <MetricValue value={fmt(values.total_taxes)} />
            </TableCell>
          </TableRow>
          <TableRow className="border-b-0 hover:bg-transparent">
            <TableCell className="py-1 pl-0 pr-2 text-xs font-semibold text-foreground">
              Total Charges
            </TableCell>
            <TableCell className="py-1 pr-0 text-right text-xs font-semibold text-[var(--red)]">
              <MetricValue value={fmt(values.total_charges)} />
            </TableCell>
          </TableRow>
        </TableFooter>
      </Table>
    </div>
  );
}
