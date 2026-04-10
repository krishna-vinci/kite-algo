import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import OptionsPage from "@/app/(app)/options/page";

describe("options page", () => {
  it("renders the rebuilt options workspace", () => {
    render(<OptionsPage />);

    expect(screen.getByRole("heading", { name: /strategy-aware options workspace/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /login to broker/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /option chain/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /strategy builder/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /trading dock/i })).toBeInTheDocument();
  });

  it("shows payoff and dry-run workflow in strategy builder", () => {
    render(<OptionsPage />);

    expect(screen.getByText(/primary structured deployment workflow/i)).toBeInTheDocument();
    expect(screen.getByText(/backend-defined rule inputs/i)).toBeInTheDocument();
    expect(screen.getByText(/basket MTM/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /payoff chart with day and scenario sliders/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /build dry-run plan/i })).toBeInTheDocument();
  });
});
