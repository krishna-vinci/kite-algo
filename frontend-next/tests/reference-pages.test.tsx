import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";
import AlgosPage from "@/app/(app)/algos/page";
import AlertsPage from "@/app/(app)/alerts/page";
import OptionsPage from "@/app/(app)/options/page";
import ScreenersPage from "@/app/(app)/screeners/page";

describe("reference pages", () => {
  it("renders the dashboard shell content", () => {
    render(<DashboardPage />);

    expect(screen.getByRole("heading", { name: "Operator overview" })).toBeInTheDocument();
    expect(screen.getByText("Execution stream")).toBeInTheDocument();
    expect(screen.getByText("Pinned symbols")).toBeInTheDocument();
  });

  it("renders the options strategy builder", () => {
    render(<OptionsPage />);

    expect(screen.getByRole("heading", { name: "Strategy builder" })).toBeInTheDocument();
    expect(screen.getByLabelText("delta search")).toHaveValue("0.30");
    expect(screen.getByText("Option chain")).toBeInTheDocument();
  });

  it("renders the tmux-style algos page", () => {
    render(<AlgosPage />);

    expect(screen.getByRole("heading", { name: "Process manager" })).toBeInTheDocument();
    expect(screen.getByText("tmux")).toBeInTheDocument();
    expect(screen.getByText("Live task stream")).toBeInTheDocument();
  });

  it("renders alerts quick create and history", () => {
    render(<AlertsPage />);

    expect(screen.getByRole("heading", { name: "New alert" })).toBeInTheDocument();
    expect(screen.getByText("Active alerts")).toBeInTheDocument();
    expect(screen.getByText("Alert history")).toBeInTheDocument();
  });

  it("renders saved screeners and results", () => {
    render(<ScreenersPage />);

    expect(screen.getByText("Saved screeners")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Filter builder" })).toBeInTheDocument();
    expect(screen.getByText("Matched symbols")).toBeInTheDocument();
  });
});
