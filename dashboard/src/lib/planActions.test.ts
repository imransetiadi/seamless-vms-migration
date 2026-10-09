import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { PLAN_STATUSES, type Migration, type Plan, type PlanStatus } from '../api/types';
import { planActions, type PlanActionKey } from './planActions';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const base = fx.plans.find((p) => p.id === 'plan-c81d44a0') as Plan;
const planWith = (status: PlanStatus): Plan => ({ ...base, status });
const ready: Migration[] = fx.migrations.filter((m) => m.plan_id === base.id);

const EXPECTED: Record<PlanStatus, Record<PlanActionKey, boolean>> = {
  draft: { validate: true, waves: true, start: false, pause: false, edit: true },
  validated: { validate: true, waves: true, start: true, pause: false, edit: true },
  running: { validate: false, waves: false, start: false, pause: true, edit: false },
  paused: { validate: true, waves: false, start: true, pause: false, edit: false },
  completed: { validate: false, waves: false, start: false, pause: false, edit: false },
  failed: { validate: true, waves: false, start: false, pause: false, edit: false },
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

  it('requires the operator role for every plan action', () => {
    const actions = planActions(planWith('validated'), 'viewer', ready);
    for (const key of ['validate', 'waves', 'start', 'pause'] as const) {
      expect(actions[key].enabled).toBe(false);
      expect(actions[key].reason).toMatch(/operator role/);
    }
    expect(planActions(planWith('validated'), 'admin', ready).start.enabled).toBe(true);
  });
});
