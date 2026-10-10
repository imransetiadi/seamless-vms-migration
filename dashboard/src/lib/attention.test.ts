import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { attentionItems } from './attention';
import { completedSince } from './finalize';

const NOW = Date.parse('2026-10-08T12:00:00Z');

function scenario() {
  const fx = buildFixtures(NOW);
  const plans = new Map(fx.plans.map((p) => [p.id, p]));
  const byName = (name: string): Migration => {
    const m = fx.migrations.find((x) => x.vm.name === name);
    if (!m) throw new Error(name);
    return m;
  };
  return { fx, plans, byName };
}

describe('attentionItems', () => {
  it('flags a completed migration whose source VM runs again, and one waiting for finalize past its plan reminder (SDD §8.1, ADV-19)', () => {
    const { fx, plans, byName } = scenario();
    const cache = byName('web-cache-01');
    expect(cache.phase).toBe('completed');
    expect(NOW - Date.parse(completedSince(cache) ?? '')).toBeGreaterThan(72 * 3_600_000);
    const find = () => attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === cache);
    expect(find()).toMatchObject({
      tone: 'warning',
      reason: 'Waiting for finalize longer than the plan allows (72 h)',
      nextAction: 'Finalize or roll it back (approver)',
      rank: 6,
    });
    // the plan sets the reminder; without the plan it is unknown, so nothing is claimed
    const plan = plans.get(cache.plan_id)!;
    plan.finalize_warning_h = 24 * 30;
    expect(find()).toBeUndefined();
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === cache)).toBeUndefined();

    // a source VM running again: its data diverges from RHOSO, plan or not
    cache.source_running_at = '2026-10-08T11:00:00Z';
    const running = { tone: 'danger', reason: 'The source VM runs again: its data diverges from RHOSO', nextAction: 'Stop the source VM, or roll it back (operator)', rank: 1 };
    expect(find()).toMatchObject(running);
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === cache)).toMatchObject(running);
    // only while it waits in Completed
    cache.phase = 'finalized';
    expect(find()).toBeUndefined();
  });

  it('ranks a failed cutover with a stopped source first', () => {
    const { fx, plans } = scenario();
    const items = attentionItems(fx.migrations, plans, NOW);
    expect(items[0]?.migration.vm.name).toBe('legacy-rhel6-app');
    expect(items[0]?.reason).toMatch(/source VM is stopped/);
  });

  it('flags single-shot migrations at the cutover gate only when their wave is active (SDD §5.4)', () => {
    const { fx, plans, byName } = scenario();
    // wave-3 depends on wave-1 and wave-2, which are still running.
    byName('api-gw-02').phase = 'ready';
    // approved: what is left is the cutover request (a pending approval is in Pending approvals, ADV-20)
    for (const name of ['ad-dc-01', 'api-gw-02']) byName(name).approvals = [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }];
    const names = attentionItems(fx.migrations, plans, NOW).map((i) => i.migration.vm.name);
    expect(names).toContain('ad-dc-01'); // wave-2 has no dependencies
    expect(names).not.toContain('api-gw-02');
  });

  it('distinguishes waiting for approval from waiting for a cutover request', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    // waiting for an approval: listed once on the Overview, in Pending approvals (SDD §16, ADV-20)
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)).toBeUndefined();
    // the requester's own approval does not count (maker-checker, SDD §5.4): still an approval to give, even
    // while the window is closed — the window is a wait after the approval, not instead of it
    plans.get(billing.plan_id)!.cutover_window = { start: '2026-10-08T20:00:00Z', end: '2026-10-08T23:00:00Z' };
    billing.cutover_requested = true;
    billing.cutover_requested_by = 'rina';
    billing.approvals = [{ actor: 'rina', at: '2026-10-08T11:58:00Z', comment: null }];
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)).toBeUndefined();
    billing.cutover_requested = false;
    billing.cutover_requested_by = null;
    billing.approvals = [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }];
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)?.reason).toMatch(/cutover request/);
  });
});
