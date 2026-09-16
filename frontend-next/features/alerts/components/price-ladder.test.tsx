// features/alerts/components/price-ladder.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ladderRange, PriceLadder } from "./price-ladder";

describe("ladderRange", () => {
  it("pads both sides of the price-target span", () => {
    const range = ladderRange(100, 110);
    expect(range).toEqual({ min: 97.5, max: 112.5 });
  });

  it("is symmetric regardless of which value is higher", () => {
    expect(ladderRange(110, 100)).toEqual({ min: 97.5, max: 112.5 });
  });

  it("keeps a visible window when price and target nearly touch", () => {
    const range = ladderRange(100, 100.0001)!;
    expect(range.min).toBeLessThan(100);
    expect(range.max).toBeGreaterThan(100.0001);
  });

  it("with no target, shows a small band around the price", () => {
    expect(ladderRange(100, null)).toEqual({ min: 95, max: 105 });
  });

  it("returns null without a usable price", () => {
    expect(ladderRange(null as unknown as number, 100)).toBeNull();
    expect(ladderRange(Number.NaN, 100)).toBeNull();
  });
});

function markerPct(testId: string): number {
  const el = screen.getByTestId(testId);
  return parseFloat((el as HTMLElement).style.left);
}

describe("PriceLadder", () => {
  it("places the target marker beyond the price marker, both inside the track", () => {
    render(
      <PriceLadder
        price={100}
        target={110}
        distance={{
          distance: 10,
          percent: 10,
          direction: "above",
          sentence: "Target is ₹10.00 above the current price (+10.00%).",
          alreadyBeyond: false,
        }}
      />,
    );
    const pricePct = markerPct("ladder-price-marker");
    const targetPct = markerPct("ladder-target-marker");
    expect(pricePct).toBeGreaterThan(0);
    expect(pricePct).toBeLessThan(100);
    expect(targetPct).toBeGreaterThan(pricePct);
    expect(targetPct).toBeLessThan(100);
    expect(screen.getByText(/₹10\.00 above the current price/)).toBeTruthy();
  });

  it("shows a price-only track and a set-the-level hint without a target", () => {
    render(<PriceLadder price={100} target={null} distance={null} />);
    expect(screen.getByTestId("ladder-price-marker")).toBeTruthy();
    expect(screen.queryByTestId("ladder-target-marker")).toBeNull();
    expect(screen.getByText(/set a level/i)).toBeTruthy();
  });

  it("renders nothing but the waiting hint without a price", () => {
    render(<PriceLadder price={null} target={100} distance={null} />);
    expect(screen.queryByTestId("ladder-price-marker")).toBeNull();
    expect(screen.getByText(/waiting for the first price/i)).toBeTruthy();
  });
});
