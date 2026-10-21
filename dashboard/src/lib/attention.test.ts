import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration } from '../api/types';
import { pendingApprovals } from './approvals';
import { attentionItems } from './attention';
import { completedSince } from './finalize';
import { summaryOf } from './summary';

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
      // a summary carries no checkpoint: once a finalize began deleting the source the API refuses a rollback, so the
      // hint says when one still stands (SDD §8.1, review RV52-D2)
      nextAction: 'Finalize it, or roll it back while no finalize has begun (approver; see its page)',
      rank: 6,
    });
    // the plan sets the reminder; without the plan it is unknown, so nothing is claimed
    const plan = plans.get(cache.plan_id)!;
    plan.finalize_warning_h = 24 * 30;
    expect(find()).toBeUndefined();
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === cache)).toBeUndefined();

    // a source VM running again: its data diverges from the destination, plan or not
    cache.source_running_at = '2026-10-08T11:00:00Z';
    const running = { tone: 'danger', reason: 'The source VM runs again: its data diverges from the destination', nextAction: 'Stop the source VM, roll it back, or finalize it again (see its page)', rank: 1 };
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

  it('offers no Cancel for a failed migration whose source was stopped once, and a failed rollback its rollback (review RV32A-05)', () => {
    const { byName } = scenario();
    const base = byName('app-billing-01');
    const closed = { phase: 'failed' as const, downtime_started_at: '2026-10-08T11:00:00Z', downtime_ended_at: '2026-10-08T11:20:00Z', cutover_refusals: [] };
    const [rolled] = attentionItems([{ ...base, ...closed, error: 'rollback: rollback: dst: HTTP 400' }], new Map(), NOW);
    expect(rolled?.nextAction).toMatch(/roll back again/i);
    expect(rolled?.nextAction).not.toMatch(/cancel/i);
    const [ran] = attentionItems([{ ...base, ...closed, error: 'cutover: stop source server: HTTP 500' }], new Map(), NOW);
    expect(ran?.nextAction).not.toMatch(/cancel/i);
    const open = { phase: 'failed' as const, downtime_started_at: null, downtime_ended_at: null, cutover_refusals: [], error: 'precopy: x' };
    const [never] = attentionItems([{ ...base, ...open }], new Map(), NOW);
    expect(never?.nextAction).toMatch(/cancel/i);
  });

  it('ranks a failed rollback with a closed clock with a stopped source, and offers the cancel to one never stopped (review RV34B-03)', () => {
    const { byName } = scenario();
    const base = byName('app-billing-01');
    // nothing tells whether the rollback started the source before it failed: it may still be stopped
    const closed = { phase: 'failed' as const, downtime_started_at: '2026-10-08T11:00:00Z', downtime_ended_at: '2026-10-08T11:20:00Z', cutover_refusals: [] };
    const [rolled] = attentionItems([{ ...base, ...closed, error: 'rollback: start the source server: HTTP 503' }], new Map(), NOW);
    expect(rolled).toMatchObject({ tone: 'danger', rank: 0, nextAction: 'Check whether the source VM runs, then roll back again (operator)' });
    const open = { phase: 'failed' as const, downtime_started_at: null, downtime_ended_at: null, cutover_refusals: [], error: 'rollback: delete the server in rhoso: HTTP 503' };
    const [never] = attentionItems([{ ...base, ...open }], new Map(), NOW);
    expect(never).toMatchObject({ rank: 1, nextAction: 'Roll back again or cancel (operator)' });
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

  it('lists a rolled-back migration last: it waits for a person to retry or cancel it (SDD §16)', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    billing.phase = 'rolled_back';
    billing.error = 'cutover: simulated cutover failure: destination volume attach timed out on the conversion host';
    const items = attentionItems(fx.migrations, plans, NOW);
    const item = items.find((i) => i.migration === billing);
    expect(item).toMatchObject({ tone: 'info', reason: 'Rolled back — the source VM runs again', nextAction: 'Fix the cause, then retry or cancel (operator)', rank: 7 });
    // last, after every more urgent entry (the overdue finalizes included): only rolled-back ones share its rank
    expect(items.filter((i) => i.migration.phase !== 'rolled_back').every((i) => i.rank < 7)).toBe(true);
    expect(items.indexOf(item!)).toBeGreaterThan(items.map((i) => i.rank < 7).lastIndexOf(true));
    // listed whatever is known of its plan
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === billing)?.rank).toBe(7);
    // cancelled, nothing waits for anyone
    billing.phase = 'cancelled';
    expect(attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing)).toBeUndefined();
  });

  it('lists a warm migration whose cutover will refuse, ahead of what it waits for (SDD §16)', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    expect(billing.phase).toBe('awaiting_cutover');
    const find = () => attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing);
    expect(find()).toBeUndefined(); // waiting for an approval: Pending approvals lists it
    const refusal = "the NIC fa:16:3e:00:00:01 of app-billing-01 on 'finance-app': port p-1 holds 10.0.0.5";
    // fixing what the cutover will refuse is another human action: Needs attention lists it, its approval stays
    // in Pending approvals
    billing.cutover_refusals = [refusal];
    expect(find()).toMatchObject({ tone: 'warning', reason: 'Cutover will refuse 1 problem', nextAction: 'Fix it, then Sync now (operator)', rank: 3 });
    billing.cutover_refusals = [refusal, refusal.replace('10.0.0.5', '10.0.0.6')];
    expect(find()).toMatchObject({ reason: 'Cutover will refuse 2 problems', nextAction: 'Fix them, then Sync now (operator)' });
    // while its passes run, the next pass looks again
    billing.phase = 'syncing';
    expect(find()).toMatchObject({ reason: 'Cutover will refuse 2 problems', nextAction: 'Fix them before it converges: the next pass looks again (operator)' });
    billing.phase = 'precopy';
    expect(find()?.reason).toBe('Cutover will refuse 2 problems');
    // retried, it is ready again: its pre-copy looks again before anything waits on it
    billing.phase = 'ready';
    expect(find()).toBeUndefined();
    billing.phase = 'precopy';
    // the problems are the migration's own: listed whatever is known of its plan
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === billing)?.reason).toBe(
      'Cutover will refuse 2 problems',
    );
    // a failure and a review come first; once a pass finds none, it waits as before
    billing.phase = 'awaiting_cutover';
    billing.review_required = true;
    expect(find()?.reason).toMatch(/^Review required/);
    billing.review_required = false;
    billing.phase = 'failed';
    billing.error = `cutover: ${refusal}`;
    expect(find()?.reason).toBe(`Failed: cutover: ${refusal}`);
    billing.phase = 'awaiting_cutover';
    billing.cutover_refusals = [];
    expect(find()).toBeUndefined();
  });

  it('ranks a wait for GPU capacity as a wait, not an approval', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    expect(billing.phase).toBe('awaiting_cutover');
    const plan = plans.get(billing.plan_id)!;
    // the gate's other rules hold - an approval that counts, a cutover request, no window: nothing waits for a person
    plan.cutover_window = null;
    billing.approvals = [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }];
    billing.cutover_requested = true;
    const find = () => attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing);
    expect(find()).toBeUndefined();
    // a capacity read found no room (SDD §5.4 rule 5, §22.7): listed as a wait, its source still running
    billing.gpu_wait = {
      since: '2026-10-08T11:58:00Z',
      checked_at: '2026-10-08T11:59:30Z',
      pool: 'VGPU CUSTOM_NVIDIA_A100_3_20C',
      needed: 1,
      free: 0,
      reason: "VGPU CUSTOM_NVIDIA_A100_3_20C of RHOSO 18.0 — Production has 0 units free, and flavor 'g1.a100-3-20c' needs 1",
    };
    const waiting = {
      tone: 'info',
      reason: 'Waiting for GPU capacity: VGPU CUSTOM_NVIDIA_A100_3_20C, 0 free, needs 1',
      nextAction: 'Wait, or free GPU capacity on the destination: the cutover starts once a read finds room',
      rank: 6,
    };
    expect(find()).toMatchObject(waiting);
    // ranked with the window's wait, never with the human actions ahead of it, and never in Pending approvals
    const dc = byName('ad-dc-01'); // cold, ready, in the same running plan: approved and requested, its window closed
    Object.assign(dc, { approvals: [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }], cutover_requested: true });
    plan.cutover_window = { start: '2026-10-08T20:00:00Z', end: '2026-10-08T23:00:00Z' };
    const window = attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === dc);
    expect(window?.reason).toMatch(/^Waiting for the cutover window/);
    expect(window?.rank).toBe(find()?.rank);
    plan.cutover_window = null;
    expect(pendingApprovals(fx.migrations, plans).items.map((i) => i.migration.vm.name)).not.toContain('app-billing-01');
    // the same wait in a single-shot migration's ready and in a keep-warm pass
    billing.phase = 'syncing';
    expect(find()).toMatchObject(waiting);
    billing.phase = 'awaiting_cutover';
    // a read that failed names the failure in place of the numbers it could not read (SDD §22.12)
    billing.gpu_wait = { ...billing.gpu_wait, free: null, reason: 'the GPU capacity of RHOSO 18.0 — Production could not be read (HTTP 503)' };
    expect(find()?.reason).toBe('Waiting for GPU capacity: the GPU capacity of RHOSO 18.0 — Production could not be read (HTTP 503)');
    // what its cutover will refuse is listed ahead of it, as ahead of any wait
    billing.cutover_refusals = ["the NIC fa:16:3e:00:00:01 of app-billing-01 on 'finance-app': port p-1 holds 10.0.0.5"];
    expect(find()?.reason).toBe('Cutover will refuse 1 problem');
    billing.cutover_refusals = [];
    // a summary carries it as the migration does (SDD §4.2)
    expect(attentionItems([summaryOf(billing)], plans, NOW)[0]?.reason).toBe(find()?.reason);
    // the entry is a GPU element: it carries the Technology Preview badge (SDD §16)
    expect(find()?.techPreview).toBe(true);
    expect(attentionItems(fx.migrations, plans, NOW).filter((i) => i.techPreview).map((i) => i.migration)).toEqual([billing]);
  });

  it('lists what the gate waits for first while its other rules no longer hold beside a wait for GPU capacity (SDD §5.4, §16, G12 review)', () => {
    const { fx, plans, byName } = scenario();
    const billing = byName('app-billing-01');
    const plan = plans.get(billing.plan_id)!;
    plan.cutover_window = null;
    billing.approvals = [{ actor: 'sari', at: '2026-10-08T11:59:00Z', comment: null }];
    billing.cutover_requested = true;
    billing.gpu_wait = { since: '2026-10-08T11:58:00Z', checked_at: '2026-10-08T11:59:30Z', pool: 'VGPU', needed: 1, free: 0, reason: 'VGPU of RHOSO 18.0 — Production has 0 units free' };
    const find = () => attentionItems(fx.migrations, plans, NOW).find((i) => i.migration === billing);
    expect(find()?.reason).toMatch(/^Waiting for GPU capacity/);
    // the window closed again: the gate reads no capacity until it opens, its wait stays as it was
    plan.cutover_window = { start: '2026-10-08T20:00:00Z', end: '2026-10-08T23:00:00Z' };
    expect(find()?.reason).toMatch(/^Waiting for the cutover window/);
    expect(find()?.techPreview).toBeUndefined();
    plan.cutover_window = null;
    // no cutover request any more: the request is what it waits for
    billing.cutover_requested = false;
    expect(find()?.reason).toBe('Approved — waiting for a cutover request');
    billing.cutover_requested = true;
    // an approval that does not count is Pending approvals', listed once there
    billing.approvals = [];
    expect(find()).toBeUndefined();
    expect(pendingApprovals(fx.migrations, plans).items.map((i) => i.migration)).toContain(billing);
    // without its plan nothing tells: the wait is what it records
    expect(attentionItems(fx.migrations, new Map(), NOW, { plansUnknown: true }).find((i) => i.migration === billing)?.reason).toMatch(/^Waiting for GPU capacity/);
  });
});
