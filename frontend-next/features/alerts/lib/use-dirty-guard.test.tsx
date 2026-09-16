// features/alerts/lib/use-dirty-guard.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useDirtyGuard } from "./use-dirty-guard";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

// vitest.config.ts clears no mocks automatically; without this the first
// test's `push("/alerts")` leaks into the dirty-path assertions below.
beforeEach(() => {
  push.mockClear();
});

function Harness({ isDirty }: { isDirty: boolean }) {
  const { attemptExit, dialog } = useDirtyGuard(isDirty);
  return (
    <div>
      <button type="button" onClick={() => attemptExit("/alerts")}>
        leave
      </button>
      {dialog}
    </div>
  );
}

describe("useDirtyGuard", () => {
  it("navigates immediately when the draft is clean", () => {
    render(<Harness isDirty={false} />);
    fireEvent.click(screen.getByText("leave"));
    expect(push).toHaveBeenCalledWith("/alerts");
    expect(screen.queryByText("Leave without saving?")).toBeNull();
  });

  it("opens the dialog when dirty; leaving confirms, keeping editing stays", async () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    render(<Harness isDirty={true} />);
    expect(addSpy).toHaveBeenCalledWith("beforeunload", expect.any(Function));

    fireEvent.click(screen.getByText("leave"));
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByText("Leave without saving?")).toBeTruthy();

    fireEvent.click(screen.getByText("Keep editing"));
    expect(push).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("leave"));
    fireEvent.click(screen.getByText("Leave without saving"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/alerts"));
  });
});
