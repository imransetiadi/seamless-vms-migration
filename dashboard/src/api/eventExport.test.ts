import { describe, expect, it } from 'vitest';
import { createTestClient, createTestServer } from '../test/utils';
import { exportEventTrail, jsonLineParts } from './eventExport';

describe('exportEventTrail (SDD §16: the whole audit trail, like `seamless events export`)', () => {
  it('reads every persisted event page by page, in seq order', async () => {
    const server = createTestServer();
    const api = createTestClient(server, 'viewer');
    const progress: number[] = [];
    const events = await exportEventTrail(api, { pageSize: 40, onProgress: (n) => progress.push(n) });

    const persisted = server.events.filter((e) => e.seq > 0);
    expect(persisted.length).toBeGreaterThan(100); // several pages of 40
    expect(events.map((e) => e.seq)).toEqual(persisted.map((e) => e.seq));
    expect(progress.at(-1)).toBe(persisted.length);
    expect(progress.length).toBe(Math.floor(persisted.length / 40) + 1);
  });

  it('exports one plan’s events when a plan is chosen', async () => {
    const server = createTestServer();
    const api = createTestClient(server, 'viewer');
    const events = await exportEventTrail(api, { planId: 'plan-4f2a9c1e', pageSize: 25 });
    const expected = server.events.filter((e) => e.seq > 0 && e.plan_id === 'plan-4f2a9c1e');
    expect(expected.length).toBeGreaterThan(25);
    expect(events.map((e) => e.seq)).toEqual(expected.map((e) => e.seq));
  });

  it('stops when asked, between pages', async () => {
    const server = createTestServer();
    const api = createTestClient(server, 'viewer');
    const stop = new AbortController();
    const run = exportEventTrail(api, { pageSize: 10, signal: stop.signal, onProgress: (n) => n >= 20 && stop.abort() });
    await expect(run).rejects.toThrow();
  });

  it('writes JSON lines, one event per line, like the command, as one part per event', () => {
    const server = createTestServer();
    const [a, b] = server.events;
    // parts, never one string: a trail of millions of events is longer than a string may be (security re-audit)
    expect(jsonLineParts([a!, b!])).toEqual([`${JSON.stringify(a)}\n`, `${JSON.stringify(b)}\n`]);
  });
});
