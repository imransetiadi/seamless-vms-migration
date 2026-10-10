import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { PLAN_STATUSES, type Migration, type Plan, type PlanStatus } from '../api/types';
import { planActions, type PlanActionKey } from './planActions';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const base = fx.plans.find((p) => p.id === 'plan-c81d44a0') as Plan;
const planWith = (status: PlanStatus): Plan => ({ ...base, status });
const ready: Migration[] = fx.migrations.filter((m) => m.plan_id === base.id);

// finalize needs a completed migration (and an approver): none of these has one
const EXPECTED: Record<PlanStatus, Record<PlanActionKey, boolean>> = {
  draft: { validate: true, waves: true, start: false, pause: false, edit: true, finalize: false },
  validated: { validate: true, waves: true, start: true, pause: false, edit: true, finalize: false },
  running: { validate: false, waves: false, start: false, pause: true, edit: false, finalize: false },
  // the API plans waves on a paused plan too, returning it to draft (SDD §12)
  paused: { validate: true, waves: true, start: true, pause: false, edit: false, finalize: false },
  completed: { validate: false, waves: false, start: false, pause: false, edit: false, finalize: false },
  // a plan fails when its pre-staging fails; Start pre-stages it again (SDD §8)
  failed: { validate: true, waves: false, start: true, pause: false, edit: false, finalize: false },
};

describe('planActions', () => {
  it.each(PLAN_STATUSES)('enables the right actions for a %s plan (operator)', (status) => {
    const actions = planActions(planWith(status), 'operator', ready);
    for (const [key, enabled] of Object.entries(EXPECTED[status]) as Array<[PlanActionKey, boolean]>) {
      expect(actions[key].enabled, `${key} in ${status}`).toBe(enabled);
      if (!enabled) expect(actions[key].reason, `${key} in ${status}`).toBeTruthy();
    }
  });

  it('asks to validate a draft before starting', () => {
    expect(planActions(planWith('draft'), 'operator', ready).start.reason).toMatch(/validate/i);
  });

  it('refuses to start while a migration is blocked (the API answers 409)', () => {
    const blocked = ready.map((m, i) => (i === 0 ? { ...m, phase: 'blocked' as const } : m));
    const start = planActions(planWith('validated'), 'operator', blocked).start;
    expect(start.enabled).toBe(false);
    expect(start.reason).toMatch(/1 migration is blocked/);
  });

  it('refuses to start a waved plan while an unfinished migration belongs to no wave, naming it (SDD §9.4)', () => {
    const plan: Plan = { ...planWith('validated'), waves: [{ ...base.waves[0]!, id: 'wave-a' }] };
    const [one, two, three] = fx.migrations.slice(0, 3).map((m) => ({ ...m, plan_id: plan.id, phase: 'ready' as const }));
    const migrations: Migration[] = [
      { ...one!, wave_id: 'wave-a' },
      { ...two!, wave_id: null, vm: { ...two!.vm, name: 'web-09' } }, // added after the waves were planned
      { ...three!, wave_id: 'wave-gone', vm: { ...three!.vm, name: 'web-10' } }, // its wave was planned away
    ];
    const start = planActions(plan, 'operator', migrations).start;
    expect(start.enabled).toBe(false);
    expect(start.reason).toBe('2 migrations belong to no wave: web-09, web-10 — plan the waves again or add them to a wave.');
  });

  it('starts a waved plan whose migrations outside every wave have finished, and a plan without waves', () => {
    const plan: Plan = { ...planWith('validated'), waves: [{ ...base.waves[0]!, id: 'wave-a' }] };
    const [one, two] = fx.migrations.slice(0, 2).map((m) => ({ ...m, plan_id: plan.id }));
    const finished: Migration[] = [
      { ...one!, wave_id: 'wave-a', phase: 'ready' },
      { ...two!, wave_id: null, phase: 'cancelled' },
    ];
    expect(planActions(plan, 'operator', finished).start.enabled).toBe(true);
    const unwaved: Migration[] = [{ ...one!, wave_id: null, phase: 'ready' }];
    expect(planActions({ ...plan, waves: [] }, 'operator', unwaved).start.enabled).toBe(true);
  });

  it('starts a failed plan again only when no migration is blocked, like a validated one (SDD §8)', () => {
    const blocked = ready.map((m, i) => (i === 0 ? { ...m, phase: 'blocked' as const } : m));
    const start = planActions(planWith('failed'), 'operator', blocked).start;
    expect(start.enabled).toBe(false);
    expect(start.reason).toMatch(/1 migration is blocked/);
  });

  it('refuses to edit or re-plan the waves while a migration is in flight, naming it (SDD §12)', () => {
    const waiting = ready.map((m, i) => (i === 0 ? { ...m, phase: 'awaiting_cutover' as const } : m));
    const actions = planActions(planWith('validated'), 'operator', waiting);
    expect(actions.edit.enabled).toBe(false);
    expect(actions.edit.reason).toContain(waiting[0]!.vm.name);
    expect(actions.waves.enabled).toBe(false);
    expect(actions.waves.reason).toMatch(/in flight/i);
    // a failed migration whose source runs keeps the plan editable: fix the cause, then retry
    const failed = ready.map((m, i) => (i === 0 ? { ...m, phase: 'failed' as const, downtime_started_at: null } : m));
    expect(planActions(planWith('validated'), 'operator', failed).edit.enabled).toBe(true);
  });

  it('says what releases each migration that keeps the plan from changing (SDD §12)', () => {
    const named = (m: Migration, i: number, phase: Migration['phase'], stopped = false): Migration => ({
      ...m,
      vm: { ...m.vm, name: `vm-${String(i).padStart(2, '0')}` },
      phase,
      downtime_started_at: stopped ? '2026-10-10T12:00:00Z' : null,
      downtime_ended_at: null,
    });
    // a completed migration is finished and cannot be cancelled: finalize or roll back (SDD §8.1)
    const done = Array.from({ length: 5 }, (_, i) => named(ready[0]!, 5 - i, 'completed'));
    const actions = planActions(planWith('validated'), 'operator', done);
    expect(actions.edit.reason).toBe('Finalize or roll back the completed migrations first: vm-01, vm-02, vm-03 and 2 more.');
    expect(actions.waves.reason).toBe(actions.edit.reason);
    // most urgent first: a failed cutover that stopped the source, then the ones in flight
    const mixed = [named(ready[0]!, 3, 'completed'), named(ready[0]!, 2, 'syncing'), named(ready[0]!, 1, 'failed', true)];
    expect(planActions(planWith('validated'), 'operator', mixed).edit.reason).toBe(
      'Roll back or retry the failed migration whose source VM is stopped first: vm-01. ' +
        'Finish, roll back or cancel the migration in flight first: vm-02. ' +
        'Finalize or roll back the completed migration first: vm-03.',
    );
    // another plan's migrations leave this plan alone
    const elsewhere = { ...named(ready[0]!, 4, 'completed'), plan_id: 'plan-other001' };
    expect(planActions(planWith('validated'), 'operator', [...ready, elsewhere]).edit).toEqual({ enabled: true, reason: null });
  });

  it('offers Start to no plan whose migrations all ended, whatever its stored status says (SDD §8.1, §16, ADV-19)', () => {
    const done = ready.map((m) => ({ ...m, phase: 'completed' as const }));
    // a re-validated finished plan: stored validated, derived completed
    const finished = { ...planWith('validated'), derived_status: 'completed' as const };
    expect(planActions(finished, 'operator', done).start).toEqual({ enabled: false, reason: 'The plan has completed: finalize or roll back its VMs.' });
    const finalized = { ...planWith('completed'), derived_status: 'finalized' as const };
    expect(planActions(finalized, 'operator', done.map((m) => ({ ...m, phase: 'finalized' as const }))).start.reason).toBe('The plan is finalized.');
  });

  it('offers Finalize to an approver while a migration awaits finalize (SDD §8.1, ADV-19)', () => {
    const done = ready.map((m, i) => ({ ...m, phase: i === 0 ? ('completed' as const) : ('finalized' as const) }));
    const plan = { ...planWith('completed'), derived_status: 'completed' as const };
    expect(planActions(plan, 'approver', done).finalize).toEqual({ enabled: true, reason: null });
    expect(planActions(plan, 'operator', done).finalize).toEqual({ enabled: false, reason: 'Requires the approver role.' });
    const none = done.map((m) => ({ ...m, phase: 'finalized' as const }));
    expect(planActions(plan, 'approver', none).finalize).toEqual({ enabled: false, reason: 'No VM awaits finalize.' });
  });

  it('requires the operator role for every plan action', () => {
    const actions = planActions(planWith('validated'), 'viewer', ready);
    for (const key of ['validate', 'waves', 'start', 'pause'] as const) {
      expect(actions[key].enabled).toBe(false);
      expect(actions[key].reason).toMatch(/operator role/);
    }
    expect(planActions(planWith('validated'), 'admin', ready).start.enabled).toBe(true);
  });
});
