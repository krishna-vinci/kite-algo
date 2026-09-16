// features/alerts/components/live-side-panel.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LiveSidePanel, type PanelSummary } from "./live-side-panel";
import type { ValidationResult } from "@/features/alerts/hooks/use-definition-validation";

const emptyValidation: ValidationResult = {
  state: "ready",
  issues: [],
  bySection: {
    instrument: [],
    rule: [],
    evaluation: [],
    frequency: [],
    destinations: [],
    other: [],
  },
  previewSentence: null,
  preview: null,
  error: null,
  revalidate: () => {},
};

const summary: PanelSummary = {
  title: "You are creating",
  name: "RELIANCE crosses above ₹1,500",
  rule: "RELIANCE crosses above 1500",
  frequency: "Once, then it goes quiet.",
  channels: ["telegram-desk"],
};

describe("LiveSidePanel", () => {
  it("shows the price with freshness, age and receipts", () => {
    render(
      <LiveSidePanel
        quote={{ age_ms: 1000, exchange_timestamp: "2026-09-16T09:15:00Z", received_at: "2026-09-16T09:15:01Z" } as never}
        presentation={{ price: 124860, tone: "positive", label: "LIVE" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("₹1,24,860.00")).toBeTruthy();
    expect(screen.getByText("LIVE")).toBeTruthy();
    expect(screen.getByText(/updated 1s ago/)).toBeTruthy();
    expect(screen.getByText(/exchange .*received/)).toBeTruthy();
  });

  it("shows coverage text instead of one price for universe alerts", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage="50 instruments in NIFTY50"
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("50 instruments in NIFTY50")).toBeTruthy();
    expect(screen.queryByTestId("ladder-price-marker")).toBeNull();
  });

  it("renders the preview sentence and validation state at readable size", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: 100, tone: "positive", label: "LIVE" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={{ ...emptyValidation, state: "ready", previewSentence: "Price is already above the level." }}
        summary={summary}
      />,
    );
    expect(screen.getByText("Valid")).toBeTruthy();
    expect(screen.getByText("Price is already above the level.")).toBeTruthy();
  });

  it("links each issue group to its section anchor", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={{
          ...emptyValidation,
          state: "invalid",
          bySection: { ...emptyValidation.bySection, rule: ["The condition is not valid."] },
        }}
        summary={summary}
      />,
    );
    const link = screen.getByText("Condition").closest("a");
    expect(link?.getAttribute("href")).toBe("#section-rule");
    expect(screen.getByText("The condition is not valid.")).toBeTruthy();
  });

  it("summarises exactly what will be saved", () => {
    render(
      <LiveSidePanel
        quote={null}
        presentation={{ price: null, tone: "neutral", label: "NO DATA" }}
        coverage={null}
        targetValue={null}
        distance={null}
        validation={emptyValidation}
        summary={summary}
      />,
    );
    expect(screen.getByText("You are creating")).toBeTruthy();
    expect(screen.getByText("Once, then it goes quiet.")).toBeTruthy();
    expect(screen.getByText(/telegram-desk/)).toBeTruthy();
  });
});
