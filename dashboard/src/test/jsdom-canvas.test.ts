import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

// jsdom implements no canvas: its getContext reports "Not implemented" to the window's virtual console,
// which Vitest prints as an error with a stack trace, and answers null. axe-core probes a canvas for
// icon ligatures, so every a11y check printed that trace in a green run, hiding real failures in the
// scroll-back (audit F-10). The setup answers null as jsdom does, without the report.

type VirtualConsole = { on(event: string, listener: (error: Error) => void): void; off(event: string, listener: (error: Error) => void): void };

describe('a canvas in the test environment (audit F-10)', () => {
  const reports: string[] = [];
  const listener = (error: Error) => reports.push(error.message);
  // jsdom's own console for the window: what Vitest prints its reports from. Read through the public
  // JSDOM#virtualConsole of the instance Vitest exposes as `jsdom`: the window field it used to be read from
  // (`window._virtualConsole`) is private, and jsdom 30 moved it.
  const virtualConsole = (globalThis as unknown as { jsdom?: { virtualConsole?: VirtualConsole } }).jsdom?.virtualConsole;

  beforeEach(() => {
    reports.length = 0;
    virtualConsole?.on('jsdomError', listener);
  });

  afterEach(() => {
    virtualConsole?.off('jsdomError', listener);
    document.body.innerHTML = '';
  });

  it('runs in a jsdom window whose virtual console the test can hear', () => {
    expect(virtualConsole).toBeDefined();
  });

  it("answers getContext with null and no 'not implemented' report", () => {
    const context = document.createElement('canvas').getContext('2d');

    expect(context).toBeNull();
    expect(reports.filter((message) => message.includes('Not implemented'))).toEqual([]);
  });

  it("lets axe-core probe a control's visible text for icon ligatures without that report", async () => {
    // label-content-name-mismatch compares a control's aria-label with its visible text, which it
    // measures on a canvas to leave icon ligatures out: the probe that printed the trace
    document.body.innerHTML =
      '<main><h1>Plans</h1><button type="button" aria-label="Add a plan">add</button></main>';

    const results = await axe.run(document.body, {
      runOnly: { type: 'rule', values: ['label-content-name-mismatch'] },
    });

    expect(results.passes.length + results.incomplete.length + results.violations.length).toBeGreaterThan(0);
    expect(reports.filter((message) => message.includes('Not implemented'))).toEqual([]);
  });
});
