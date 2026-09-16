// features/alerts/components/live-side-panel.tsx
"use client";

/**
 * The editor's right rail: everything the system knows about the draft being
 * built, in one sticky column — the live price, where the target sits, what
 * the definition would do right now, what is still wrong (linked to the
 * section that fixes it), and a reading of exactly what will be saved.
 *
 * It renders state the editor already computed; it computes nothing itself.
 */

import Link from "next/link";
import { AlertCircleIcon } from "lucide-react";

import { StatusBadge } from "@/components/operator/status-badge";
import { PriceLadder } from "@/features/alerts/components/price-ladder";
import {
  SECTION_META,
  VALIDATION_LABEL,
  VALIDATION_TONE,
} from "@/features/alerts/lib/status";
import { formatAge, formatPrice, type TargetDistance } from "@/features/alerts/lib/plain-language";
import type { ValidationResult } from "@/features/alerts/hooks/use-definition-validation";

export type PanelSummary = {
  title: string;
  name: string;
  rule: string;
  frequency: string;
  channels: string[];
};

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2 rounded-xl border border-border/70 bg-card/60 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function LiveSidePanel({
  quote,
  presentation,
  coverage,
  targetValue,
  distance,
  validation,
  summary,
}: {
  quote: {
    age_ms: number | null | undefined;
    exchange_timestamp: string | null | undefined;
    received_at: string | null | undefined;
    change_percent: number | null | undefined;
  } | null;
  presentation: { price: number | null; tone: "positive" | "warning" | "danger" | "neutral"; label: string };
  coverage: string | null;
  targetValue: number | null;
  distance: TargetDistance | null;
  validation: ValidationResult;
  summary: PanelSummary;
}) {
  const stateLabel = VALIDATION_LABEL[validation.state] ?? validation.state;
  const stateTone = VALIDATION_TONE[validation.state] ?? "neutral";

  return (
    <div className="flex flex-col gap-4">
      <Card title="Live market">
        {coverage ? (
          <p className="text-sm text-muted-foreground">{coverage}</p>
        ) : (
          <div className="flex flex-col gap-1">
            <span className="flex items-baseline gap-2">
              <span className="text-2xl font-semibold tabular-nums">
                {formatPrice(presentation.price)}
              </span>
              <StatusBadge tone={presentation.tone}>{presentation.label}</StatusBadge>
            </span>
            {quote ? (
              <>
                <span className="text-xs text-muted-foreground">{formatAge(quote.age_ms)}</span>
                <span className="text-xs text-muted-foreground">
                  {quote.exchange_timestamp
                    ? `exchange ${new Date(quote.exchange_timestamp).toLocaleTimeString()}`
                    : "no exchange timestamp"}
                  {quote.received_at
                    ? ` · received ${new Date(quote.received_at).toLocaleTimeString()}`
                    : ""}
                </span>
                {quote.change_percent !== null && quote.change_percent !== undefined ? (
                  <span className="text-xs text-muted-foreground">
                    {quote.change_percent >= 0 ? "+" : ""}
                    {quote.change_percent.toFixed(2)}% today
                  </span>
                ) : null}
              </>
            ) : null}
            <PriceLadder price={presentation.price} target={targetValue} distance={distance} />
          </div>
        )}
      </Card>

      <Card title="What this alert will do">
        <span className="flex items-center gap-2">
          <StatusBadge tone={stateTone}>{stateLabel}</StatusBadge>
        </span>
        {validation.previewSentence ? (
          <p className="text-sm">{validation.previewSentence}</p>
        ) : null}
        {validation.error ? (
          <p className="text-xs text-amber-300" role="status">
            {validation.error} Your draft is untouched.
          </p>
        ) : null}
        {SECTION_META.map(({ section, label, anchor }) => {
          const messages = validation.bySection[section] ?? [];
          if (messages.length === 0) return null;
          return (
            <div key={section} className="flex flex-col gap-1">
              <Link href={`#${anchor}`} className="text-xs font-medium underline text-muted-foreground">
                {label}
              </Link>
              <ul className="flex flex-col gap-1 text-xs text-rose-300" role="alert">
                {messages.map((message) => (
                  <li key={message} className="flex items-start gap-1">
                    <AlertCircleIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
                    {message}
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </Card>

      <Card title={summary.title}>
        <p className="text-sm font-medium">{summary.name}</p>
        <p className="text-xs text-muted-foreground">{summary.rule}</p>
        <p className="text-xs text-muted-foreground">{summary.frequency}</p>
        <p className="text-xs text-muted-foreground">
          {summary.channels.length > 0
            ? `Via ${summary.channels.join(", ")}`
            : "No destination selected yet."}
        </p>
      </Card>
    </div>
  );
}
