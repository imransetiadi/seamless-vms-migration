import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { approvalKindLabel, pendingApprovals } from './approvals';

const NOW = Date.parse('2026-10-08T12:00:00Z');

function scenario() {
  const fx = buildFixtures(NOW);
  const plans = new Map(fx.plans.map((p) => [p.id, p]));
  const byName = (name: string): Migration => {
    const m = fx.migrations.find((x) => x.vm.name === name);
    if (!m) throw new Error(name);
    return m;
  };
  const find = (m: Migration) => pendingApprovals(fx.migrations, plans).items.find((i) => i.migration === m);
  return { fx, plans, byName, find };
}

describe('pending approvals, both kinds (SDD §16, ADV-20)', () => {
  it('lists the cutover approvals waiting at the gate and the advisor decisions on hold, each with its kind', () => {
    const { fx, plans, byName } = scenario();
    const { items, cutoverUnknown } = pendingApprovals(fx.migrations, plans);
    expect(cutoverUnknown).toBe(false);
    expect(items.map((i) => [i.kind, i.migration.vm.name, i.waitsFor, i.href])).toEqual([
      ['cutover', 'ad-dc-01', 'Ready for cutover: waiting for an approval.', `/migrations/${byName('ad-dc-01').id}`],
      ['cutover', 'app-billing-01', 'Converged: waiting for an approval; keep-warm syncs run meanwhile.', `/migrations/${byName('app-billing-01').id}`],
      ['advisor_decision', 'analytics-node-2', "Approve or override the advisor's decision (admin); nothing starts before that.", '/advisor/recommendations/rec-b2c4d6e87101'],
    ]);
    expect(items.map(approvalKindLabel)).toEqual(['Cutover approval', 'Cutover approval', 'Advisor decision · Migration strategy']);
    expect(items.map((i) => i.plan?.id)).toEqual(['plan-4f2a9c1e', 'plan-4f2a9c1e', 'plan-c81d44a0']);
  });

  it("counts only the approvals the gate counts: the requester's own does not (maker-checker, SDD §5.4)", () => {
    const { byName, find } = scenario();
    const billing = byName('app-billing-01');
    billing.cutover_requested = true;
    billing.cutover_requested_by = 'rina';
    billing.approvals = [{ actor: 'rina', at: '2026-10-08T11:50:00Z', comment: null }];
    expect(find(billing)?.waitsFor).toBe('Requested by rina: another approver approves it.');
    billing.approvals.push({ actor: 'sari', at: '2026-10-08T11:55:00Z', comment: null });
    expect(find(billing)).toBeUndefined();
  });

  it('lists a forced cutover waiting for a second approver, and nothing away from the gate', () => {
    const { byName, find } = scenario();
    const pg = byName('db-pg-01'); // a warm pre-copy
    expect(find(pg)).toBeUndefined();
    pg.force_cutover = { actor: 'rina', at: '2026-10-08T11:00:00Z', reason: 'The change window closes in 20 minutes', delta_bytes: 2 ** 30, estimated_downtime_s: 320 };
    pg.cutover_requested = true;
    pg.cutover_requested_by = 'rina';
    pg.approvals = [{ actor: 'rina', at: '2026-10-08T11:00:00Z', comment: 'The change window closes in 20 minutes' }];
    expect(find(pg)?.waitsFor).toBe('Cutover forced by rina: another approver approves it.');
    // a warm migration in ready waits for its wave, not at the gate
    expect(find(byName('analytics-db'))).toBeUndefined();
    // nor in a running plan whose wave runs: it pre-copies and converges first (SDD §5.4)
    const mysql = byName('db-mysql-02');
    mysql.phase = 'ready';
    expect(find(mysql)).toBeUndefined();
  });

  it('lists a single-shot migration only once its wave runs, and none where the plan needs no approval', () => {
    const { fx, plans, byName, find } = scenario();
    // wave-3 depends on wave-1 and wave-2, which still run
    const gw = byName('api-gw-02');
    gw.phase = 'ready';
    expect(find(gw)).toBeUndefined();
    expect(find(byName('ad-dc-01'))).toBeDefined();
    plans.get('plan-4f2a9c1e')!.status = 'paused';
    expect(find(byName('ad-dc-01'))).toBeUndefined();
    plans.get('plan-4f2a9c1e')!.status = 'running';
    plans.get('plan-4f2a9c1e')!.require_approval = false;
    expect(pendingApprovals(fx.migrations, plans).items.filter((i) => i.kind === 'cutover')).toEqual([]);
  });

  it('says the cutover approvals are unknown without the plans, and still lists the advisor decisions', () => {
    const { fx } = scenario();
    const { items, cutoverUnknown } = pendingApprovals(fx.migrations, new Map(), { plansUnknown: true });
    expect(cutoverUnknown).toBe(true);
    expect(items.map((i) => [i.kind, i.migration.vm.name, i.plan])).toEqual([['advisor_decision', 'analytics-node-2', undefined]]);
  });
});
