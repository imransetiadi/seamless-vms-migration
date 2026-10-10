import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §8.1).
import raw from '../../../seamless/tests/fixtures/plan_status_cases.json?raw';
import { buildFixtures } from './mockData';
import { derivePlanStatus } from './mockPlanStatus';
import { DERIVED_PLAN_STATUSES, type Migration, type Phase, type PlanStatus } from './types';

const fixture = JSON.parse(raw) as { cases: Array<{ stored: PlanStatus; phases: Phase[]; expected: string; note?: string }> };
const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const basePlan = fx.plans[0]!;
const baseMigration = fx.migrations[0]!;

describe('the derived plan status in the mock, like the control plane (SDD §8.1, ADV-19)', () => {
  it.each(fixture.cases.map((c) => [`${c.stored} with ${c.phases.join(', ') || 'no migration'}`, c] as const))('%s', (_name, c) => {
    const plan = { ...basePlan, id: 'plan-test0001', status: c.stored };
    const migrations: Migration[] = c.phases.map((phase, i) => ({ ...baseMigration, id: `mig-${i}`, plan_id: plan.id, phase }));
    expect(derivePlanStatus(plan, migrations)).toBe(c.expected);
  });

  it('covers every status', () => {
    expect(new Set(fixture.cases.map((c) => c.expected))).toEqual(new Set(DERIVED_PLAN_STATUSES));
  });
});
