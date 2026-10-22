import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard (SDD §5.4, §16)
import raw from '../../../seamless/tests/fixtures/forced_estimate_cases.json?raw';
import estimateRaw from '../../../seamless/tests/fixtures/estimate_cases.json?raw';
import { buildFixtures, estimate as mockEstimate } from '../api/mockData';
import { DOWNTIME_PARTS, type Estimate, type Migration, type Plan, type Strategy, type SyncPass, type VMRef } from '../api/types';
import {
  estimateStrategy,
  finalDowntime,
  firstPassSeconds,
  forcedDowntime,
  paramsFor,
  type PlanEstimatorSettings,
} from './forcedEstimate';

interface Case {
  name: string;
  vm: VMRef;
  strategy: Strategy;
  plan: PlanEstimatorSettings;
  observed_scan_bps: number | null;
  last_pass_s: number | null;
  /** The post_create hooks' part of the migration's estimate, none when not given (SDD §9.1, plan task N10). */
  hooks_s?: number;
  expected_s: number;
}

const cases = JSON.parse(raw) as Case[];

describe("the Force cutover's estimated downtime, like the control plane (SDD §5.4, §16)", () => {
  it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const p = paramsFor(c.plan, c.observed_scan_bps);
    const last = c.last_pass_s ?? firstPassSeconds(c.vm, c.strategy, p);
    expect(finalDowntime(c.vm, c.strategy, last, p, c.hooks_s ?? 0)).toBeCloseTo(c.expected_s, 9);
  });

  it("adds the post_create hooks' part of the migration's estimate (SDD §9.1, plan task N10)", () => {
    const c = cases.find((x) => x.hooks_s)!;
    const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0] as Migration;
    const estimate = { ...base.estimate!, downtime_parts: { ...base.estimate!.downtime_parts, hooks_s: c.hooks_s } };
    const m = { ...base, vm: c.vm, strategy: c.strategy, observed_scan_bps: c.observed_scan_bps, sync_passes: [] as SyncPass[] };
    expect(forcedDowntime({ ...m, estimate }, c.plan)).toBeCloseTo(c.expected_s, 9);
    expect(forcedDowntime({ ...m, estimate }, c.plan) - forcedDowntime(m, c.plan)).toBeCloseTo(c.hooks_s!, 9);
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
    // the pass still running is not one that ended: the last that ended counts - its whole time, a
    // warm pass recording the part after its snapshot (SDD §9.1 T_last, review RV39-07)
    // (1000 s: long enough for the fast writer's delta, not the scan, to set the copy)
    const passes = [pass(1, 1200, at), pass(2, 1000, at), pass(3, 9999, null)];
    const p = paramsFor(c.plan, c.observed_scan_bps);
    expect(forcedDowntime({ ...m, sync_passes: passes }, c.plan)).toBeCloseTo(finalDowntime(c.vm, c.strategy, 1000 + p.snapshot_s, p), 9);
    // a CBT pass records its whole time
    const cbt = { ...m, strategy: 'vmware_warm' as Strategy, sync_passes: passes };
    expect(forcedDowntime(cbt, c.plan)).toBeCloseTo(finalDowntime(c.vm, 'vmware_warm', 1000, p), 9);
  });

  it('does not drop when the expected first pass ends as expected (SDD §5.4, review RV39-07)', () => {
    const c = cases.find((x) => x.last_pass_s === null && x.strategy === 'warm')!;
    const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0] as Migration;
    const m = { ...base, vm: c.vm, strategy: c.strategy, observed_scan_bps: c.observed_scan_bps, sync_passes: [] as SyncPass[] };
    const p = paramsFor(c.plan, c.observed_scan_bps);
    const at = '2026-10-08T11:00:00Z';
    // the first pass as long as expected records the part after its snapshot
    const ended: SyncPass = {
      number: 1, kind: 'full', started_at: at, ended_at: at, bytes_scanned: 0, bytes_changed: 0, bytes_transferred: 0,
      duration_s: firstPassSeconds(c.vm, c.strategy, p) - p.snapshot_s,
    };
    expect(forcedDowntime({ ...m, sync_passes: [ended] }, c.plan)).toBeCloseTo(forcedDowntime(m, c.plan), 9);
  });
});

interface EstimateCase {
  name: string;
  vm: VMRef;
  strategy: Strategy;
  plan: PlanEstimatorSettings & Pick<Plan, 'downtime_slo_s'>;
  expected: Omit<Estimate, 'eligible' | 'reasons'>;
}

// One case table for the control plane's estimate and the dashboard's port of it (SDD §9.1, §16)
const estimateCases = JSON.parse(estimateRaw) as EstimateCase[];
const tenth = (n: number) => Math.round(n * 10) / 10;

describe("each strategy's estimate, as a validation of the control plane computes it (SDD §9.1, §16)", () => {
  it.each(estimateCases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const got = estimateStrategy(c.vm, c.strategy, paramsFor(c.plan), c.plan.downtime_slo_s);
    const want = c.expected;
    expect([got.strategy, got.passes, got.final_delta_bytes, got.meets_slo]).toEqual([want.strategy, want.passes, want.final_delta_bytes, want.meets_slo]);
    for (const key of ['precopy_s', 'downtime_s', 'total_s'] as const) expect(got[key]).toBeCloseTo(want[key], 9);
    for (const part of DOWNTIME_PARTS) expect(got.downtime_parts[part]).toBeCloseTo(want.downtime_parts[part]!, 9);
  });
});

describe("the offline mock's estimates: the control plane's, each part rounded to 0.1 s (SDD §16)", () => {
  const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).plans[0] as Plan;

  it.each(estimateCases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const shown = mockEstimate(c.vm, c.strategy, { ...base, ...c.plan }, []);
    const want = c.expected;
    expect([shown.strategy, shown.passes, shown.final_delta_bytes, shown.meets_slo]).toEqual([want.strategy, want.passes, want.final_delta_bytes, want.meets_slo]);
    for (const part of DOWNTIME_PARTS) expect(shown.downtime_parts[part]).toBe(tenth(want.downtime_parts[part]!));
    // the downtime is the sum of the parts shown, so they add up (ADV-16)
    expect(shown.downtime_s).toBe(tenth(DOWNTIME_PARTS.reduce((sum, part) => sum + shown.downtime_parts[part]!, 0)));
    expect(shown.precopy_s).toBe(tenth(want.precopy_s));
    expect(shown.total_s).toBe(tenth(want.precopy_s + shown.downtime_s));
  });

  it('says why a strategy is not eligible, with the same figures', () => {
    const c = estimateCases[0]!;
    const shown = mockEstimate(c.vm, c.strategy, { ...base, ...c.plan }, ['a reason']);
    expect([shown.eligible, shown.reasons, shown.passes]).toEqual([false, ['a reason'], c.expected.passes]);
  });
});
