"use client";

import { Panel } from "@/components/operator/panel";
import { Badge } from "@/components/ui/badge";
import {
  instrumentKeyFromDocument,
  operatorLabel,
  sessionLabel,
} from "@/features/alerts/lib/authoring";

/**
 * Renders the STORED canonical document for a human.
 *
 * This is a view, never a second format: it reads whatever the document
 * contains and carries through anything it does not model (a sequence, a
 * breadth block, a pair operand) rather than dropping it. A definition the UI
 * cannot pretty-print is still shown verbatim, because silently hiding a field
 * would make the YAML tab and this view disagree.
 */

function formatOperand(operand: unknown): string {
  if (operand === null || operand === undefined) return "—";
  if (typeof operand === "number") return String(operand);
  if (typeof operand === "string") return operand;
  if (typeof operand !== "object") return JSON.stringify(operand);

  const record = operand as Record<string, unknown>;
  if ("pair_ratio" in record || "relative_strength" in record) {
    const key = "pair_ratio" in record ? "pair_ratio" : "relative_strength";
    const spec = (record[key] ?? {}) as Record<string, unknown>;
    const extra = spec.lookback ? ` over ${spec.lookback} bars` : "";
    return `${key}(${String(spec.instrument ?? "?")} / ${String(spec.reference ?? "?")}${extra})`;
  }
  if ("field" in record) return String(record.field);
  if ("indicator" in record) {
    const params = Object.entries(record)
      .filter(([key]) => key !== "indicator")
      .map(([key, value]) => `${key}=${String(value)}`)
      .join(", ");
    return params ? `${String(record.indicator)}(${params})` : String(record.indicator);
  }
  if (record.kind === "field") return String(record.name ?? "?");
  if (record.kind === "indicator") return String(record.name ?? "?");
  if (record.kind === "value") return String(record.value ?? "?");
  return JSON.stringify(operand);
}

function formatCondition(condition: unknown, index: number): string {
  if (typeof condition !== "object" || condition === null) return `condition ${index + 1}`;
  const record = condition as Record<string, unknown>;
  const left = formatOperand(record.left);
  const right = formatOperand(record.right);
  const op = String(record.op ?? "?");
  const hysteresis = record.hysteresis
    ? ` (release ${String((record.hysteresis as Record<string, unknown>).release)})`
    : "";
  return `${left} ${operatorLabel(op)} ${right}${hysteresis}`;
}

function formatGroups(block: unknown): string[] {
  if (!block || typeof block !== "object") return [];
  const record = block as Record<string, unknown>;
  const lines: string[] = [];
  for (const group of ["all", "any", "not"]) {
    const conditions = record[group];
    if (!Array.isArray(conditions)) continue;
    const joiner = group === "all" ? " and " : group === "any" ? " or " : " not ";
    lines.push(
      `${group.toUpperCase()}: ` +
        conditions.map((condition, index) => formatCondition(condition, index)).join(joiner) +
        (group === "not" ? "" : ""),
    );
  }
  return lines;
}

type StageDocument = Record<string, unknown>;

function StageCard({ stage }: Readonly<{ stage: StageDocument }>) {
  const conditionLines = formatGroups(stage.conditions);
  const anyLines = formatGroups(stage.any_conditions ? { any: stage.any_conditions } : null);
  const notLines = formatGroups(stage.not_conditions ? { not: stage.not_conditions } : null);

  return (
    <Panel tone="subtle">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm">{String(stage.id)}</span>
        <Badge variant="outline">{String(stage.type)}</Badge>
        <Badge variant="secondary">{String(stage.clock)}</Badge>
        {stage.timeframe ? <Badge variant="secondary">{String(stage.timeframe)}</Badge> : null}
        {stage.consecutive_bars ? (
          <Badge variant="secondary">{String(stage.consecutive_bars)} consecutive bars</Badge>
        ) : null}
      </div>

      {conditionLines.length > 0 ? (
        <ul className="mt-3 flex flex-col gap-1 text-sm">
          {conditionLines.map((line, index) => (
            <li key={`all-${index}`}>{line}</li>
          ))}
        </ul>
      ) : null}
      {[...anyLines, ...notLines].map((line, index) => (
        <p key={`extra-${index}`} className="mt-1 text-sm">
          {line}
        </p>
      ))}

      {/* Anything the pretty-printer does not model is shown verbatim rather
          than dropped, so this view can never disagree with the YAML tab. */}
      {stage.sequence ? (
        <div className="mt-3 rounded-md border border-border/60 p-2">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">sequence</p>
          <pre className="mt-1 overflow-auto text-xs">{JSON.stringify(stage.sequence, null, 2)}</pre>
        </div>
      ) : null}
      {stage.breadth ? (
        <div className="mt-3 rounded-md border border-border/60 p-2">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">breadth</p>
          <pre className="mt-1 overflow-auto text-xs">{JSON.stringify(stage.breadth, null, 2)}</pre>
        </div>
      ) : null}
    </Panel>
  );
}

type AlertDocument = Record<string, unknown>;

function AlertCard({ alert }: Readonly<{ alert: AlertDocument }>) {
  const extras: Array<[string, unknown]> = (
    [
      ["cooldown", alert.cooldown_s],
      [
        "rearm",
        alert.rearm_level ? `${String(alert.rearm_level)} (${String(alert.rearm_direction)})` : null,
      ],
      ["reminder", alert.reminder_interval_s],
      ["max per session", alert.max_per_session],
      ["notify if already true", alert.notify_if_already_true ? "yes" : null],
    ] as Array<[string, unknown]>
  ).filter(([, value]) => value !== undefined && value !== null && value !== false);

  return (
    <Panel tone="subtle">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm">{String(alert.id)}</span>
        <Badge variant="outline">trigger: {String(alert.trigger)}</Badge>
        <span className="text-xs text-muted-foreground">source: {String(alert.source)}</span>
      </div>
      <p className="mt-2 text-sm">
        Channels:{" "}
        {Array.isArray(alert.channels) && alert.channels.length > 0
          ? alert.channels.map(String).join(", ")
          : "none"}
      </p>
      {extras.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
          {extras.map(([label, value]) => (
            <li key={label}>
              {label}: <span className="text-foreground">{String(value)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Panel>
  );
}

export function ReadableDefinition({
  document,
}: Readonly<{ document: Record<string, unknown> | null }>) {
  if (!document) {
    return <p className="text-sm text-muted-foreground">No definition is stored for this workflow.</p>;
  }

  const stages = Array.isArray(document.stages) ? (document.stages as StageDocument[]) : [];
  const alerts = Array.isArray(document.alerts) ? (document.alerts as AlertDocument[]) : [];
  // The canonical document stores instruments as objects ({symbol, exchange}),
  // so `String()` on them renders "[object Object]": use the identity helper.
  const instruments = Array.isArray(document.instruments)
    ? document.instruments
        .map((entry) => instrumentKeyFromDocument(entry) ?? "")
        .filter((key) => key !== "")
    : [];

  return (
    <div className="flex flex-col gap-4">
      <Panel tone="subtle">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-xs text-muted-foreground">name</dt>
            <dd className="font-mono">{String(document.name ?? "—")}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">session</dt>
            {/* Human label; the canonical value stays in the document. */}
            <dd>{sessionLabel(String(document.session ?? "")) || "—"}</dd>
          </div>
          <div className="col-span-2">
            <dt className="text-xs text-muted-foreground">coverage</dt>
            <dd className="font-mono">
              {document.universe
                ? `universe: ${JSON.stringify(document.universe)}`
                : instruments.length > 0
                  ? instruments.join(", ")
                  : "none"}
            </dd>
          </div>
        </dl>
      </Panel>

      <div className="flex flex-col gap-2">
        <h3 className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Stages</h3>
        {stages.map((stage) => (
          <StageCard key={String(stage.id)} stage={stage} />
        ))}
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Alerts</h3>
        {alerts.length === 0 ? (
          <p className="text-sm text-muted-foreground">No alerts in this definition.</p>
        ) : (
          alerts.map((alert) => <AlertCard key={String(alert.id)} alert={alert} />)
        )}
      </div>

      {document.screener ? (
        <div className="flex flex-col gap-2">
          <h3 className="text-xs uppercase tracking-[0.2em] text-muted-foreground/60">Screener</h3>
          <pre className="overflow-auto rounded-lg border border-border/60 p-3 text-xs">
            {JSON.stringify(document.screener, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}
