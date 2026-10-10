import { describe, expect, it } from 'vitest';
import type { AdvisorPolicy } from '../api/types';
import { resolvePolicy } from './policies';

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
