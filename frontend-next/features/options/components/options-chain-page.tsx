"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { OPTION_UNDERLYINGS } from "@/lib/options/types";
import type { OptionChainRow, OptionContractView, OptionUnderlying } from "@/lib/options/types";
import {
  useOptionChain,
  useOptionExpiries,
  useOptionMaxPain,
  useOptionPcr,
  useOptionSession,
  useStartOptionSession,
} from "@/features/options/hooks/use-options-queries";
import { computePayoffProfile, type OptionSide, type PayoffLeg } from "@/features/options/lib/payoff";

function fmtNum(value: number | null | undefined, dp = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Math.round(value).toLocaleString("en-IN");
}

/** One clickable contract cell (LTP) inside a chain row. */
function ContractCell({
  contract,
  onPick,
}: Readonly<{
  contract: OptionContractView | null;
  onPick: (side: OptionSide) => void;
}>) {
  if (!contract) {
    return <TableCell className="text-muted-foreground">—</TableCell>;
  }
  return (
    <TableCell>
      <div className="flex flex-col gap-0.5">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => onPick("BUY")}
            className="rounded px-1.5 py-0.5 font-medium text-emerald-600 hover:bg-emerald-500/10 dark:text-emerald-400"
            title="Add BUY leg"
          >
            {fmtNum(contract.ltp)}
          </button>
          <button
            type="button"
            onClick={() => onPick("SELL")}
            className="rounded px-1 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
            title="Add SELL leg"
          >
            S
          </button>
        </div>
        <div className="text-[11px] text-muted-foreground">
          IV {fmtNum(contract.iv, 1)} · Δ {fmtNum(contract.delta, 2)} · OI {fmtInt(contract.oi)}
        </div>
      </div>
    </TableCell>
  );
}

let legIdCounter = 0;
function nextLegId(): string {
  legIdCounter += 1;
  return `leg-${Date.now()}-${legIdCounter}`;
}

export function OptionsChainPage() {
  const [underlying, setUnderlying] = useState<OptionUnderlying>("NIFTY");
  const [expiry, setExpiry] = useState<string | null>(null);
  const [legs, setLegs] = useState<PayoffLeg[]>([]);

  const sessionQuery = useOptionSession(underlying);
  const startSession = useStartOptionSession(underlying);
  const hasSession = sessionQuery.data != null;

  const expiriesQuery = useOptionExpiries(underlying, hasSession);
  const expiries = expiriesQuery.data?.expiries ?? sessionQuery.data?.expiries ?? [];

  // Auto-start a session for the underlying when none exists yet.
  useEffect(() => {
    if (sessionQuery.isSuccess && sessionQuery.data == null && !startSession.isPending) {
      startSession.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionQuery.isSuccess, sessionQuery.data, underlying]);

  // Reset the picked expiry when the underlying changes or the current one drops out.
  useEffect(() => {
    if (expiries.length === 0) return;
    if (!expiry || !expiries.includes(expiry)) {
      setExpiry(expiries[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, expiries.join(",")]);

  useEffect(() => {
    setLegs([]);
    setExpiry(null);
  }, [underlying]);

  const chainQuery = useOptionChain(underlying, expiry);
  const pcrQuery = useOptionPcr(underlying, expiry);
  const maxPainQuery = useOptionMaxPain(underlying, expiry);

  const chain = chainQuery.data;
  const atmStrike = chain?.atm_strike ?? null;

  function addLeg(row: OptionChainRow, optionType: "ce" | "pe", side: OptionSide) {
    const contract = row[optionType];
    if (!contract || row.strike === null || contract.ltp === null) return;
    setLegs((current) => [
      ...current,
      {
        id: nextLegId(),
        optionType: optionType === "ce" ? "CE" : "PE",
        side,
        strike: row.strike as number,
        premium: contract.ltp as number,
        lots: 1,
        lotSize: contract.lot_size ?? 1,
        delta: contract.delta,
        gamma: contract.gamma,
        theta: contract.theta,
        vega: contract.vega,
      },
    ]);
  }

  function updateLeg(id: string, patch: Partial<PayoffLeg>) {
    setLegs((current) => current.map((leg) => (leg.id === id ? { ...leg, ...patch } : leg)));
  }

  function removeLeg(id: string) {
    setLegs((current) => current.filter((leg) => leg.id !== id));
  }

  const payoff = useMemo(() => computePayoffProfile(legs), [legs]);

  return (
    <div className="flex flex-col gap-4 p-4 md:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Options</h1>
          <p className="text-sm text-muted-foreground">Live option chain, chain analytics and a payoff builder.</p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={underlying} onValueChange={(value) => setUnderlying(value as OptionUnderlying)}>
            <SelectTrigger className="w-[150px]" aria-label="Underlying">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {OPTION_UNDERLYINGS.map((u) => (
                <SelectItem key={u} value={u}>
                  {u}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={expiry ?? ""} onValueChange={setExpiry} disabled={expiries.length === 0}>
            <SelectTrigger className="w-[150px]" aria-label="Expiry">
              <SelectValue placeholder="Expiry" />
            </SelectTrigger>
            <SelectContent>
              {expiries.map((exp) => (
                <SelectItem key={exp} value={exp}>
                  {exp}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Spot</CardDescription>
            <CardTitle className="text-2xl">{fmtNum(chain?.spot_ltp)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Put/Call ratio</CardDescription>
            <CardTitle className="text-2xl">{fmtNum(pcrQuery.data?.value, 2)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Max pain</CardDescription>
            <CardTitle className="text-2xl">{fmtNum(maxPainQuery.data?.value, 0)}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Option chain</CardTitle>
          <CardDescription>
            Click an LTP to add a BUY leg to the payoff builder; click &ldquo;S&rdquo; to add a SELL leg.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {chainQuery.isLoading || sessionQuery.isLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : chainQuery.isError ? (
            <p className="text-sm text-destructive">Failed to load the option chain.</p>
          ) : !chain || chain.chain.length === 0 ? (
            <p className="text-sm text-muted-foreground">No chain data yet — session is starting.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead colSpan={2} className="text-center">
                    CE
                  </TableHead>
                  <TableHead className="text-center">Strike</TableHead>
                  <TableHead colSpan={2} className="text-center">
                    PE
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {chain.chain.map((row) => {
                  const isAtm = atmStrike !== null && row.strike !== null && Math.abs(row.strike - atmStrike) < 1e-6;
                  return (
                    <TableRow key={row.strike} className={cn(isAtm && "bg-primary/5")}>
                      <ContractCell contract={row.ce} onPick={(side) => addLeg(row, "ce", side)} />
                      <TableCell className="text-xs text-muted-foreground">
                        {fmtNum(row.ce?.oi, 0)}
                      </TableCell>
                      <TableCell className="text-center font-medium">
                        {fmtInt(row.strike)}
                        {isAtm && (
                          <Badge variant="secondary" className="ml-2">
                            ATM
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {fmtNum(row.pe?.oi, 0)}
                      </TableCell>
                      <ContractCell contract={row.pe} onPick={(side) => addLeg(row, "pe", side)} />
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Payoff builder</CardTitle>
          <CardDescription>Payoff at expiry for the selected legs.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {legs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No legs selected yet. Click an LTP in the chain above.</p>
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Side</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Strike</TableHead>
                    <TableHead>Premium</TableHead>
                    <TableHead>Lots</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {legs.map((leg) => (
                    <TableRow key={leg.id}>
                      <TableCell>
                        <Button
                          type="button"
                          size="sm"
                          variant={leg.side === "BUY" ? "default" : "destructive"}
                          onClick={() => updateLeg(leg.id, { side: leg.side === "BUY" ? "SELL" : "BUY" })}
                        >
                          {leg.side}
                        </Button>
                      </TableCell>
                      <TableCell>{leg.optionType}</TableCell>
                      <TableCell>{fmtInt(leg.strike)}</TableCell>
                      <TableCell>{fmtNum(leg.premium)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => updateLeg(leg.id, { lots: Math.max(1, leg.lots - 1) })}
                          >
                            −
                          </Button>
                          <span className="w-6 text-center">{leg.lots}</span>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => updateLeg(leg.id, { lots: leg.lots + 1 })}
                          >
                            +
                          </Button>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Button type="button" size="sm" variant="ghost" onClick={() => removeLeg(leg.id)}>
                          Remove
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Card>
                  <CardHeader className="pb-1">
                    <CardDescription>Max profit</CardDescription>
                    <CardTitle className="text-lg">
                      {payoff.maxProfit === null ? "Unlimited" : fmtNum(payoff.maxProfit, 0)}
                    </CardTitle>
                  </CardHeader>
                </Card>
                <Card>
                  <CardHeader className="pb-1">
                    <CardDescription>Max loss</CardDescription>
                    <CardTitle className="text-lg">
                      {payoff.maxLoss === null ? "Unlimited" : fmtNum(Math.abs(payoff.maxLoss), 0)}
                    </CardTitle>
                  </CardHeader>
                </Card>
                <Card>
                  <CardHeader className="pb-1">
                    <CardDescription>Breakevens</CardDescription>
                    <CardTitle className="text-lg">
                      {payoff.breakevens.length ? payoff.breakevens.map((b) => fmtInt(b)).join(", ") : "—"}
                    </CardTitle>
                  </CardHeader>
                </Card>
                <Card>
                  <CardHeader className="pb-1">
                    <CardDescription>Net Δ / Θ / Vega</CardDescription>
                    <CardTitle className="text-sm">
                      {fmtNum(payoff.netGreeks.delta, 1)} / {fmtNum(payoff.netGreeks.theta, 1)} /{" "}
                      {fmtNum(payoff.netGreeks.vega, 1)}
                    </CardTitle>
                  </CardHeader>
                </Card>
              </div>

              <div className="h-72 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={payoff.points}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                    <XAxis dataKey="spot" tickFormatter={(v: number) => fmtInt(v)} />
                    <YAxis tickFormatter={(v: number) => fmtInt(v)} />
                    <Tooltip
                      formatter={(value) => fmtNum(Number(value), 0)}
                      labelFormatter={(label) => `Spot ${fmtInt(Number(label))}`}
                    />
                    <ReferenceLine y={0} stroke="currentColor" opacity={0.4} />
                    {chain?.spot_ltp != null && (
                      <ReferenceLine x={chain.spot_ltp} stroke="hsl(221 83% 53%)" strokeDasharray="4 4" />
                    )}
                    <Line type="linear" dataKey="pnl" stroke="hsl(142 71% 45%)" dot={false} strokeWidth={2} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
