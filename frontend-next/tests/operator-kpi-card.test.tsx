import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KpiCard } from "@/components/operator/kpi-card";

describe("operator KpiCard delta tone", () => {
  it("uses green token for positive delta", () => {
    render(<KpiCard label="Net P&L" value="₹1,000" delta="+4.2%" />);
    const delta = screen.getByText("+4.2%");
    expect(delta.className).toContain("text-[var(--green)]");
  });

  it("uses red token for negative delta", () => {
    render(<KpiCard label="Max Drawdown" value="8.0%" delta="-1.4%" />);
    const delta = screen.getByText("-1.4%");
    expect(delta.className).toContain("text-[var(--red)]");
  });
});
