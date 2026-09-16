// features/alerts/components/alerts-page-header.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AlertsPageHeader } from "./alerts-page-header";

describe("AlertsPageHeader", () => {
  it("renders the trail, linking intermediate items, marking the last as current", () => {
    render(
      <AlertsPageHeader
        backHref="/alerts"
        trail={[
          { label: "Alerts", href: "/alerts" },
          { label: "Screeners", href: "/alerts/screeners" },
          { label: "momentum-scan", href: "/alerts/screeners/x" },
          { label: "Edit" },
        ]}
      />,
    );
    expect(screen.getByText("momentum-scan").getAttribute("href")).toBe("/alerts/screeners/x");
    const current = screen.getByText("Edit");
    expect(current.getAttribute("href")).toBeNull();
    expect(current.getAttribute("aria-current")).toBe("page");
  });

  // The chevron label and the trail root share the text "Alerts" by design
  // (`[← Alerts]  Alerts / …`), so these tests query by role to disambiguate.
  it("offers the way back, labelled with the first trail item", () => {
    render(
      <AlertsPageHeader backHref="/alerts" trail={[{ label: "Alerts" }, { label: "New alert" }]} />,
    );
    expect(screen.getByRole("link", { name: "Alerts" }).getAttribute("href")).toBe("/alerts");
  });

  it("calls onBack instead of navigating when the guard is wired", () => {
    const onBack = vi.fn();
    render(
      <AlertsPageHeader backHref="/alerts" trail={[{ label: "Alerts" }]} onBack={onBack} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Alerts" }));
    expect(onBack).toHaveBeenCalledWith("/alerts");
    expect(screen.queryByRole("link", { name: "Alerts" })).toBeNull();
  });

  it("renders the right-hand slot", () => {
    render(
      <AlertsPageHeader
        backHref="/alerts"
        trail={[{ label: "Alerts" }]}
        right={<button type="button">Code view</button>}
      />,
    );
    expect(screen.getByText("Code view")).toBeTruthy();
  });
});
