import { describe, expect, it } from 'vitest';
import { MockServer } from '../api/mock';
import type { Plan, Provider } from '../api/types';
import { deleteRefusal, plansThatNeed } from './providerUse';

const plan = (id: string, status: Plan['status'], counts: Plan['migration_counts'] = {}, derived = status as Plan['derived_status']) =>
  ({ id, name: `Plan ${id}`, status, derived_status: derived, migration_counts: counts, source_provider_id: 'src', destination_provider_id: 'dst' }) as Plan;

describe('plansThatNeed (SDD §12, §16)', () => {
  it('names the plans neither completed nor cancelled with their status, as source or destination', () => {
    const plans = [plan('a', 'running', { syncing: 1 }), plan('b', 'draft'), plan('c', 'failed', { failed: 1 })];
    expect(plansThatNeed('src', plans).map((u) => [u.plan.id, u.why])).toEqual([
      ['a', 'running'],
      ['b', 'draft'],
      ['c', 'failed'],
    ]);
    expect(plansThatNeed('dst', plans)).toHaveLength(3);
    expect(plansThatNeed('other', plans)).toEqual([]);
  });

  it('reads the stored status, as the API does, and says why as the plan shows it', () => {
    // a finished plan validated again is stored validated: the API still refuses, for its VMs not finalized
    const revalidated = plan('d', 'validated', { completed: 2 }, 'completed');
    expect(plansThatNeed('src', [revalidated]).map((u) => u.why)).toEqual(['2 VMs not finalized']);
    expect(plansThatNeed('src', [plan('e', 'validated', { ready: 2 })]).map((u) => u.why)).toEqual(['validated']);
    // all rolled back, so it reads Completed, but stored validated: the API refuses all the same
    expect(plansThatNeed('src', [plan('i', 'validated', { rolled_back: 2 }, 'completed')]).map((u) => u.why)).toEqual(['completed']);
  });

  it('names a completed plan only while a VM is not finalized, rolled back or cancelled', () => {
    expect(plansThatNeed('src', [plan('e', 'completed', { completed: 1, finalized: 3 })]).map((u) => u.why)).toEqual(['1 VM not finalized']);
    expect(plansThatNeed('src', [plan('f', 'completed', { completed: 2 })]).map((u) => u.why)).toEqual(['2 VMs not finalized']);
    expect(plansThatNeed('src', [plan('g', 'completed', { finalized: 2, rolled_back: 1, cancelled: 1 })])).toEqual([]);
    expect(plansThatNeed('src', [plan('h', 'cancelled', { cancelled: 2 })])).toEqual([]);
  });
});

describe('deleteRefusal (SDD §16)', () => {
  it('says which plans still need the provider and until when, three by name at most: names may hold parentheses and commas', () => {
    const users = ['a', 'b', 'c', 'd'].map((id) => ({ plan: plan(id, 'running'), why: 'running' }));
    expect(deleteRefusal(users.slice(0, 1))).toBe(
      'A plan still needs it: “Plan a” (running). A plan needs its providers until it completes and its VMs are finalized.',
    );
    expect(deleteRefusal(users)).toBe(
      'Plans still need it: “Plan a” (running), “Plan b” (running), “Plan c” (running) and 1 more. A plan needs its providers until it completes and its VMs are finalized.',
    );
    expect(deleteRefusal([])).toBeNull();
  });
});

describe('plansThatNeed agrees with the API the mock mirrors (SDD §12)', () => {
  it('predicts, for every provider, exactly the plans its DELETE is refused for', () => {
    const providers = new MockServer({ now: () => Date.parse('2026-10-08T12:00:00Z'), seed: 7 }).handle('GET', '/providers', new URLSearchParams(), undefined, 'admin').body as Provider[];
    for (const provider of providers) {
      // a fresh server each time: a DELETE that succeeds removes the provider
      const server = new MockServer({ now: () => Date.parse('2026-10-08T12:00:00Z'), seed: 7 });
      const plans = server.handle('GET', '/plans', new URLSearchParams(), undefined, 'admin').body as Plan[];
      const predicted = plansThatNeed(provider.id, plans).map((u) => u.plan.id).sort();
      const answer = server.handle('DELETE', `/providers/${provider.id}`, new URLSearchParams(), undefined, 'admin');
      const refusedFor = answer.status === 409 ? [...JSON.stringify(answer.body).matchAll(/plan-[0-9a-f]{8}/g)].map((m) => m[0]).sort() : [];
      expect([answer.status, refusedFor], provider.id).toEqual([predicted.length ? 409 : 204, predicted]);
    }
  });
});
