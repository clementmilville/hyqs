import "@testing-library/jest-dom";

// jsdom does not implement window.matchMedia — polyfill it so components
// that query viewport breakpoints (e.g. Sidebar's useIsTablet) can render.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// jsdom does not implement ResizeObserver, and reports 0 for every element's
// size, so recharts' ResponsiveContainer (e.g. PerfOverview's Stage Duration
// chart) never has a non-zero size to render into. Polyfill both so it
// measures a fixed, non-zero box and renders its children.
if (typeof window !== "undefined" && !window.ResizeObserver) {
  window.ResizeObserver = class ResizeObserver {
    constructor(callback) {
      this._callback = callback;
    }
    observe(target) {
      this._callback([{ target, contentRect: { width: 800, height: 280 } }]);
    }
    unobserve() {}
    disconnect() {}
  };
}
if (typeof Element !== "undefined" && !Element.prototype.getBoundingClientRect._hyqsPolyfilled) {
  const rect = { width: 800, height: 280, top: 0, left: 0, bottom: 280, right: 800 };
  Element.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getBoundingClientRect._hyqsPolyfilled = true;
}
