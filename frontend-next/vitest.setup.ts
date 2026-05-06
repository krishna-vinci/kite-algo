import "@testing-library/jest-dom/vitest";

// Polyfill ResizeObserver for jsdom (used by lightweight-charts and chart libs)
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
