import { describe, expect, it } from 'vitest';
import type { AdvisorPolicy } from '../api/types';
import { policyChanges, resolvePolicy } from './policies';

function pol(id: string, over: Partial<AdvisorPolicy> = {}, where: { providers?: string[]; plans?: string[] } = {}): AdvisorPolicy {
  return {
    id,
    name: id,
    scope: { provider_ids: where.providers ?? [], plan_ids: where.plans ?? [] },
    decision_types: [],
    mode: 'auto_apply_above_threshold',
    threshold: 0.6,
    min_evidence_for_auto_apply: 0,
    require_override_reason: true,
    require_distinct_approver: true,
    created_at: '2026-10-10T08:00:00Z',
    updated_at: '2026-10-10T08:00:00Z',
    updated_by: 'rina',
    ...over,
  };
}

const resolved = (policies: AdvisorPolicy[], planId = 'plan-1', providerId = 'src-1') => resolvePolicy(policies, planId, providerId, 'migration_strategy')?.id ?? 'global-default';

// the cases of the API's tests/test_policies.py, so the dashboard reads the policy in force as the API does
describe('resolvePolicy (SDD §14.6)', () => {
  it('takes the most specific level: the plan, then its source provider, then every plan, then the built-in default', () => {
    const everything = [pol('pol-global', { threshold: 0.5 }), pol('pol-provider', { threshold: 0.7 }, { providers: ['src-1'] }), pol('pol-plan', { threshold: 0.8 }, { plans: ['plan-1'] })];
    expect(resolved(everything)).toBe('pol-plan');
    expect(resolved(everything.slice(0, 2))).toBe('pol-provider');
    expect(resolved(everything.slice(0, 1))).toBe('pol-global');
    expect(resolved(everything, 'plan-2', 'src-2')).toBe('pol-global');
    expect(resolved([])).toBe('global-default');
  });

  it.each([
    ['the strictest mode, whatever the threshold', [pol('pol-a', { threshold: 0.9 }), pol('pol-b', { mode: 'always_require_approval', threshold: 0.1 })], 'pol-b'],
    ['then the higher threshold', [pol('pol-a', { threshold: 0.7 }), pol('pol-b', { threshold: 0.8 })], 'pol-b'],
    ['then the higher minimum evidence', [pol('pol-a', { min_evidence_for_auto_apply: 3 }), pol('pol-b')], 'pol-a'],
    ['then a required override reason', [pol('pol-a', { require_override_reason: false }), pol('pol-b')], 'pol-b'],
    ['then the lower id', [pol('pol-b'), pol('pol-a')], 'pol-a'],
  ])('settles a same-level conflict by %s', (_, policies, winner) => {
    const atProvider = policies.map((p) => ({ ...p, scope: { provider_ids: ['src-1'], plan_ids: [] } }));
    expect(resolved(atProvider)).toBe(winner);
  });

  it('leaves out a policy for other decision types', () => {
    const tiersOnly = pol('pol-tiers', { mode: 'always_require_approval', decision_types: ['workload_tier'] }, { providers: ['src-1'] });
    expect(resolved([tiersOnly])).toBe('global-default');
    expect(resolvePolicy([tiersOnly], 'plan-1', 'src-1', 'workload_tier')?.id).toBe('pol-tiers');
  });

  it('matches no plan or provider for a recommendation without one', () => {
    const scoped = [pol('pol-plan', {}, { plans: ['plan-1'] }), pol('pol-provider', {}, { providers: ['src-1'] })];
    expect(resolvePolicy(scoped, null, null, 'migration_strategy')).toBeNull();
  });
});

describe('policyChanges (SDD §14.5)', () => {
  const names = new Map([
    ['src-1', 'RHOSP 17.1 — DC1'],
    ['plan-1', 'Finance apps'],
  ]);

  it('lists the settings a change changed, worded as the policies page shows them and in its order', () => {
    const before = pol('pol-a', { name: 'Finance', mode: 'always_require_approval', min_evidence_for_auto_apply: 3 }, { providers: ['src-1'] });
    const after = pol(
      'pol-a',
      {
        name: 'Finance auto',
        decision_types: ['migration_strategy', 'workload_tier'],
        threshold: 0.8,
        min_evidence_for_auto_apply: 1,
        require_override_reason: false,
        require_distinct_approver: false,
        updated_at: '2026-10-11T08:00:00Z',
        updated_by: 'sari',
      },
      { plans: ['plan-1', 'plan-gone'] },
    );
    expect(policyChanges(before, after, names)).toEqual([
      { setting: 'Name', before: 'Finance', after: 'Finance auto' },
      { setting: 'When the advisor decides', before: 'Always asks for approval', after: 'Applies above the threshold' },
      // by name, by id when the page does not know it
      { setting: 'Applies to', before: 'Plans from RHOSP 17.1 — DC1', after: 'Plans Finance apps, plan-gone' },
      { setting: 'Decisions', before: 'Every kind', after: 'Migration strategy, Workload tier' },
      { setting: 'Threshold', before: '60%', after: '80%' },
      { setting: 'Minimum evidence', before: '3', after: '1' },
      { setting: 'Override reason', before: 'Required', after: 'Optional' },
      { setting: 'Distinct approver', before: 'Required', after: 'Not required' },
    ]);
    // one plan, then every plan
    expect(policyChanges(pol('pol-a', {}, { plans: ['plan-1'] }), pol('pol-a'), names)).toEqual([{ setting: 'Applies to', before: 'Plan Finance apps', after: 'Every plan' }]);
  });

  it('finds no change where only the order of a list, or who changed it when, differs', () => {
    const before = pol('pol-a', { decision_types: ['workload_tier', 'migration_strategy'] }, { plans: ['plan-1', 'plan-2'] });
    const after = pol('pol-a', { decision_types: ['migration_strategy', 'workload_tier'], updated_at: '2026-10-11T08:00:00Z', updated_by: 'sari' }, { plans: ['plan-2', 'plan-1'] });
    expect(policyChanges(before, after, names)).toEqual([]);
  });

  it('compares only what both sides hold, and nothing when a side is not a policy', () => {
    const { require_distinct_approver: _, ...older } = pol('pol-a');
    const after = pol('pol-a', { require_distinct_approver: false, threshold: 0.65 });
    expect(policyChanges(older, after, names)).toEqual([{ setting: 'Threshold', before: '60%', after: '65%' }]);
    for (const side of [null, undefined, 'pol-a', ['pol-a']]) {
      expect(policyChanges(side, after, names)).toBeNull();
      expect(policyChanges(after, side, names)).toBeNull();
    }
  });
});
