import "@testing-library/jest-dom/vitest";

// Polyfill ResizeObserver for jsdom (used by lightweight-charts and chart libs)
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// Pointer capture is a browser API jsdom does not implement. Radix Select (the
// hosted create form, the alerts editors, ...) reads it on pointerdown, so
// opening a select in a test would otherwise throw.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => undefined;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => undefined;
  }
  // Radix Select scrolls the highlighted item into view on open.
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined;
  }
}
