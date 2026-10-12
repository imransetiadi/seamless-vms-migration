import { describe, expect, it } from 'vitest';
import { MockServer } from './mock';
import { buildFixtures } from './mockData';
import type { Migration, Phase, Plan } from './types';

const NOW = Date.parse('2026-10-08T12:00:00Z');
const WARM = new Set<Migration['strategy']>(['warm', 'vmware_warm']);
/** Phases a warm migration reaches only once its passes converged (SDD §5.3). */
const CONVERGED: ReadonlySet<Phase> = new Set(['awaiting_cutover', 'cutover', 'verifying', 'completed', 'finalized']);

/**
 * The invariants of SDD §5.5, as `seamless_migrate.invariants` checks them: the codes of those a
 * migration breaks. The mock stands in for the API in the dashboard's tests and offline demo, so its
 * numbers must hold together the same way (ADV-16).
 */
function broken(m: Migration, plan: Plan, demo = true): string[] {
  const issues: string[] = [];
  const warm = WARM.has(m.strategy);
  const unrecorded = m.strategy === 'vmware_warm' && !demo;
  const final = m.sync_passes.filter((p) => p.kind === 'final').at(-1);
  if (warm && !unrecorded && (!final || (m.downtime_started_at !== null && Date.parse(final.started_at) < Date.parse(m.downtime_started_at)))) {
    issues.push('FINAL_SYNC_MISSING');
  }
  if (m.bytes_transferred !== m.sync_bytes_dropped + m.sync_passes.reduce((sum, p) => sum + p.bytes_transferred, 0)) issues.push('BYTES_MISMATCH');
  if (warm && m.converged_by !== null) {
    const last = m.sync_passes.filter((p) => p.kind !== 'final').at(-1);
    const held = {
      threshold: last !== undefined && last.bytes_changed <= plan.convergence_threshold_bytes,
      max_passes: last !== undefined && last.number >= plan.max_sync_passes,
      slo: m.estimate !== null && m.estimate.downtime_s <= plan.downtime_slo_s,
      force: m.force_cutover !== null,
    }[m.converged_by];
    if (!held) issues.push('CUTOVER_NOT_CONVERGED');
  }
  if (warm && !unrecorded && final?.duration_s != null && m.actual_downtime_s !== null && m.actual_downtime_s < final.duration_s) {
    issues.push('DOWNTIME_BELOW_FINAL_SYNC');
  }
  const parts = Object.values(m.estimate?.downtime_parts ?? {});
  if (m.estimate && parts.length > 0 && Math.abs(parts.reduce((a, b) => a + b, 0) - m.estimate.downtime_s) > 1) issues.push('ESTIMATE_PARTS_MISMATCH');
  return issues;
}

function planOf(plans: Plan[], m: Migration): Plan {
  const plan = plans.find((p) => p.id === m.plan_id);
  if (!plan) throw new Error(`no plan ${m.plan_id}`);
  return plan;
}

describe('mock data holds together like the API (SDD §5.5, ADV-16)', () => {
  const fx = buildFixtures(NOW);

  it('every completed fixture migration passes the invariants and records no issue', () => {
    const done = fx.migrations.filter((m) => m.phase === 'completed' || m.phase === 'finalized');
    expect(done.length).toBeGreaterThan(2);
    for (const m of done) {
      expect(broken(m, planOf(fx.plans, m)), m.id).toEqual([]);
      expect(m.data_issues, m.id).toEqual([]);
    }
  });

  it('every warm fixture past convergence records the rule that ended its passes, and the rule held', () => {
    const converged = fx.migrations.filter((m) => WARM.has(m.strategy) && CONVERGED.has(m.phase));
    expect(converged.length).toBeGreaterThan(3);
    for (const m of converged) {
      expect(m.converged_by, m.id).not.toBeNull();
      expect(broken(m, planOf(fx.plans, m)).filter((code) => code === 'CUTOVER_NOT_CONVERGED'), m.id).toEqual([]);
    }
    for (const m of fx.migrations.filter((x) => !CONVERGED.has(x.phase) || !WARM.has(x.strategy))) expect(m.converged_by, m.id).toBeNull();
  });

  it('records a cold copy as a full pass while the source is stopped, as the API executors do (SDD §4.2)', () => {
    const cold = fx.migrations.filter((m) => (m.strategy === 'cold' || m.strategy === 'vmware_cold') && (m.phase === 'completed' || m.phase === 'finalized'));
    expect(cold.length).toBeGreaterThan(0);
    for (const m of cold) {
      const full = m.sync_passes.find((p) => p.kind === 'full');
      expect(full, m.id).toBeDefined();
      expect(Date.parse(full!.started_at), m.id).toBeGreaterThanOrEqual(Date.parse(m.downtime_started_at!));
      expect(Date.parse(full!.ended_at!), m.id).toBeLessThanOrEqual(Date.parse(m.downtime_ended_at!));
    }
  });

  it('names the strategy of each earlier attempt, as a retry records it (SDD §4.2, §14.4)', () => {
    const retried = fx.migrations.filter((m) => m.attempts > 0);
    expect(retried.length).toBeGreaterThan(0);
    for (const m of retried) expect(m.attempt_strategies, m.id).toEqual(Array<Migration['strategy']>(m.attempts).fill(m.strategy));
    for (const m of fx.migrations.filter((x) => x.attempts === 0)) expect(m.attempt_strategies, m.id).toEqual([]);
  });

  it("every estimate's parts add up to its downtime, in the API's keys", () => {
    for (const m of fx.migrations) {
      for (const e of m.estimates) {
        const parts = e.downtime_parts;
        expect(Object.keys(parts).sort(), `${m.id} ${e.strategy}`).toEqual(['boot_s', 'conversion_s', 'copy_s', 'create_s', 'shutdown_s']);
        const sum = Object.values(parts).reduce((a, b) => a + b, 0);
        expect(Math.abs(sum - e.downtime_s), `${m.id} ${e.strategy}`).toBeLessThanOrEqual(1);
      }
    }
  });

  it('the simulation keeps them: every migration it completes passes the invariants', () => {
    let clock = NOW;
    const server = new MockServer({ now: () => clock, seed: 7 });
    for (const plan of server.plans) {
      plan.cutover_window = null;
      plan.require_approval = false;
      plan.auto_cutover = true;
    }
    const before = new Set(server.migrations.filter((m) => m.phase === 'completed').map((m) => m.id));
    for (let t = 0; t < 900; t += 1) {
      clock += 1000;
      server.tick();
    }
    const completed = server.migrations.filter((m) => m.phase === 'completed' && !before.has(m.id));
    expect(completed.length).toBeGreaterThan(2);
    for (const m of completed) {
      expect(broken(m, planOf(server.plans, m)), m.id).toEqual([]);
      if (WARM.has(m.strategy)) expect(m.converged_by, m.id).not.toBeNull();
    }
  });
});
