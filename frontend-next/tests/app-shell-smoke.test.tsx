import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppShell } from "@/components/app-shell";
import { navigation } from "@/lib/navigation";

describe("AppShell", () => {
  it("renders the terminal shell chrome", () => {
    render(
      <AppShell navigation={navigation} activeHref="/custom-display">
        <section aria-label="smoke content">
          <h1>Custom Display</h1>
        </section>
      </AppShell>,
    );

    expect(screen.getByText("K")).toBeInTheDocument();
    expect(screen.getByLabelText("command palette")).toBeInTheDocument();
    expect(screen.getByTitle("Custom Display")).toHaveAttribute("href", "/custom-display");
    expect(screen.getByText("CUSTOM DISPLAY")).toBeInTheDocument();
    expect(screen.getByText("positions")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Custom Display" })).toBeInTheDocument();
  });
});
