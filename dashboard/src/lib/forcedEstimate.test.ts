import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard (SDD §5.4, §16)
import raw from '../../../seamless/tests/fixtures/forced_estimate_cases.json?raw';
import { buildFixtures } from '../api/mockData';
import type { Migration, Strategy, SyncPass, VMRef } from '../api/types';
import { finalDowntime, firstPassSeconds, forcedDowntime, paramsFor, type PlanEstimatorSettings } from './forcedEstimate';

interface Case {
  name: string;
  vm: VMRef;
  strategy: Strategy;
  plan: PlanEstimatorSettings;
  observed_scan_bps: number | null;
  last_pass_s: number | null;
  expected_s: number;
}

const cases = JSON.parse(raw) as Case[];

describe("the Force cutover's estimated downtime, like the control plane (SDD §5.4, §16)", () => {
  it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const p = paramsFor(c.plan, c.observed_scan_bps);
    const last = c.last_pass_s ?? firstPassSeconds(c.vm, c.strategy, p);
    expect(finalDowntime(c.vm, c.strategy, last, p)).toBeCloseTo(c.expected_s, 9);
  });

  it('takes the last pass that ended, or the expected first pass while none has', () => {
    const c = cases.find((x) => x.last_pass_s === null && x.strategy === 'warm')!;
    const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0] as Migration;
    const m = { ...base, vm: c.vm, strategy: c.strategy, observed_scan_bps: c.observed_scan_bps, sync_passes: [] as SyncPass[] };
    expect(forcedDowntime(m, c.plan)).toBeCloseTo(c.expected_s, 9);
    const at = '2026-10-08T11:00:00Z';
    const pass = (number: number, duration_s: number, ended_at: string | null): SyncPass => ({
      number, kind: number === 1 ? 'full' : 'delta', started_at: at, ended_at, bytes_scanned: 0, bytes_changed: 0, bytes_transferred: 0, duration_s,
    });
    // the pass still running is not one that ended: the last that ended counts
    const passes = [pass(1, 1200, at), pass(2, 300, at), pass(3, 9999, null)];
    const p = paramsFor(c.plan, c.observed_scan_bps);
    expect(forcedDowntime({ ...m, sync_passes: passes }, c.plan)).toBeCloseTo(finalDowntime(c.vm, c.strategy, 300, p), 9);
  });
});
