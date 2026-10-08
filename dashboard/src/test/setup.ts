import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach } from 'vitest';

// findBy*/waitFor default to 1 s; give a busy CI machine some headroom (failures still fail).
configure({ asyncUtilTimeout: 3_000 });

afterEach(() => {
  cleanup();
  try {
    window.sessionStorage.clear();
    window.localStorage.clear();
  } catch {
    // storage unavailable in this environment — nothing to clear
  }
});

// jsdom gaps used by the app (theme, charts, scroll restoration).
if (typeof window !== 'undefined') {
  if (!window.matchMedia) {
    window.matchMedia = (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => undefined,
        removeListener: () => undefined,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        dispatchEvent: () => false,
      }) as MediaQueryList;
  }
  if (!('ResizeObserver' in window)) {
    class ResizeObserverStub {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    (window as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
  }
  window.scrollTo = (() => undefined) as typeof window.scrollTo;

  // jsdom has no layout: give Recharts' ResponsiveContainer a real size so charts render their SVG
  // in tests (and axe-core checks it) instead of warning about a 0×0 container.
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect(this: HTMLElement) {
    if (this.classList.contains('recharts-responsive-container')) {
      return { x: 0, y: 0, top: 0, left: 0, right: 640, bottom: 240, width: 640, height: 240, toJSON: () => ({}) } as DOMRect;
    }
    return originalRect.call(this);
  };
}
