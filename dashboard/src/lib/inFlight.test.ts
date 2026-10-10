import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §12).
import raw from '../../../seamless/tests/fixtures/in_flight_refusal_cases.json?raw';
import { buildFixtures } from '../api/mockData';
import type { Migration, Phase } from '../api/types';
import { blocksPlanChange, inFlightRefusal } from './inFlight';

const fixture = JSON.parse(raw) as {
  cases: Array<{ migrations: Array<{ vm: string; phase: Phase; source_stopped: boolean }>; before: string; expected: string; note: string }>;
};
const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0]!;

function migration(name: string, phase: Phase, stopped: boolean): Migration {
  return {
    ...base,
    id: `mig-${name}`,
    vm: { ...base.vm, name },
    phase,
    downtime_started_at: stopped ? '2026-10-10T12:00:00Z' : null,
    downtime_ended_at: null,
  };
}

describe('the in-flight refusal, like the control plane (SDD §12)', () => {
  it.each(fixture.cases.map((c) => [c.note, c] as const))('%s', (_note, c) => {
    const busy = c.migrations.map((m) => migration(m.vm, m.phase, m.source_stopped));
    expect(busy.every(blocksPlanChange)).toBe(true);
    expect(inFlightRefusal(busy, c.before)).toBe(c.expected);
  });

  it('blocks a plan change while a migration is in flight or its source VM is stopped', () => {
    expect(blocksPlanChange(migration('a', 'completed', false))).toBe(true);
    expect(blocksPlanChange(migration('a', 'failed', true))).toBe(true);
    // a failed migration whose source runs: fix the cause, then retry
    expect(blocksPlanChange(migration('a', 'failed', false))).toBe(false);
    expect(blocksPlanChange({ ...migration('a', 'failed', true), downtime_ended_at: '2026-10-10T12:05:00Z' })).toBe(false);
    for (const phase of ['ready', 'blocked', 'finalized', 'rolled_back', 'cancelled'] as const) {
      expect(blocksPlanChange(migration('a', phase, false)), phase).toBe(false);
    }
  });
});
