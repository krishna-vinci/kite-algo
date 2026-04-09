import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ChartsPage from "@/app/(app)/charts/page";
import CustomDisplayPage from "@/app/(app)/custom-display/page";
import PaperPage from "@/app/(app)/paper/page";
import SettingsPage from "@/app/(app)/settings/page";

describe("secondary reference pages", () => {
  it("renders the paper workspace mock", () => {
    render(<PaperPage />);

    expect(screen.getByRole("heading", { name: /account selector/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /grouped books/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Paper / Intraday" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Flatten group" })).toBeInTheDocument();
  });

  it("renders the settings workspace mock", () => {
    render(<SettingsPage />);

    expect(screen.getByRole("navigation", { name: /settings sections/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /trading defaults/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /session configuration/i })).toBeInTheDocument();
    expect(screen.getByText("Daily stop")).toBeInTheDocument();
  });

  it("renders the charts placeholder", () => {
    render(<ChartsPage />);

    expect(screen.getByRole("heading", { name: /chart header/i })).toBeInTheDocument();
    expect(screen.getByText(/lightweight-charts placeholder/i)).toBeInTheDocument();
    expect(screen.getByText("mocked lightweight chart canvas")).toBeInTheDocument();
  });

  it("renders the custom display composition page", () => {
    render(<CustomDisplayPage />);

    expect(screen.getByRole("heading", { name: /operator overview/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /workspace composition/i })).toBeInTheDocument();
    expect(screen.getByText(/not a final builder contract/i)).toBeInTheDocument();
  });
});
