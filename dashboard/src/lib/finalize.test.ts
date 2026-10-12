import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import type { Migration, PhaseChange, Plan, Wave } from '../api/types';
import { completedSince, finalizedText, finalizeQueue, inScope, isOverdue, queueByWave, skippedVms } from './finalize';
import { summaryOf } from './summary';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const base = fx.migrations[0]!;
const NOW = Date.parse('2026-10-10T12:00:00Z');

function completedAt(id: string, at: string, extra: Partial<Migration> = {}): Migration {
  const history: PhaseChange[] = [
    { from_phase: 'verifying', to_phase: 'completed', at: '2026-10-01T00:00:00Z', reason: 'verification passed', actor: 'orchestrator' },
    { from_phase: 'completed', to_phase: 'rolling_back', at: '2026-10-02T00:00:00Z', reason: 'app broken', actor: 'bayu' },
    { from_phase: 'verifying', to_phase: 'completed', at, reason: 'verification passed', actor: 'orchestrator' },
  ];
  return { ...base, id, phase: 'completed', phase_history: history, ...extra };
}

describe('the finalize queue (SDD §8.1, ADV-19)', () => {
  it('counts the time in Completed from the last time it entered it', () => {
    expect(completedSince(completedAt('mig-1', '2026-10-09T12:00:00Z'))).toBe('2026-10-09T12:00:00Z');
    expect(completedSince({ ...base, phase: 'ready' })).toBeNull();
  });

  it('flags a migration that waited longer than the plan allows', () => {
    const m = completedAt('mig-1', '2026-10-07T11:00:00Z'); // 73 h before NOW
    expect(isOverdue(m, 72, NOW)).toBe(true);
    expect(isOverdue(m, 74, NOW)).toBe(false);
  });

  it("takes a summary's phase start as its completion time, as the migration's history gives it (SDD §4, perf audit PF-02)", () => {
    const m = completedAt('mig-1', '2026-10-07T11:00:00Z');
    expect(completedSince(summaryOf(m))).toBe('2026-10-07T11:00:00Z');
    expect(isOverdue(summaryOf(m), 72, NOW)).toBe(true);
    expect(isOverdue(summaryOf(m), 74, NOW)).toBe(false);
    expect(completedSince(summaryOf({ ...m, phase: 'rolling_back' }))).toBeNull();
  });

  it('scopes a wave by its VMs, as the API does, and the plan by its own migrations', () => {
    const vm = (id: string, name: string) => ({ ...base.vm, source_id: id, name });
    const a = completedAt('mig-a', '2026-10-09T10:00:00Z', { plan_id: 'plan-x', vm: vm('vm-a', 'a'), wave_id: 'wave-stale' });
    const b = completedAt('mig-b', '2026-10-08T10:00:00Z', { plan_id: 'plan-x', vm: vm('vm-b', 'b'), wave_id: 'wave-1' });
    const c = completedAt('mig-c', '2026-10-07T10:00:00Z', { plan_id: 'plan-x', vm: vm('vm-c', 'c'), wave_id: null });
    const other = completedAt('mig-o', '2026-10-01T10:00:00Z', { plan_id: 'plan-y', vm: vm('vm-a', 'a') });
    const wave = (id: string, order: number, vmIds: string[]): Wave => ({ id, name: `Wave ${order}`, order, vm_ids: vmIds, depends_on: [], max_parallel: 5 });
    const w1 = wave('wave-1', 1, ['vm-b']);
    const w2 = wave('wave-2', 2, ['vm-a']);
    // the waves in their order, whatever the list order
    const plan = { ...(fx.plans[0] as Plan), id: 'plan-x', waves: [w2, w1] };
    const all = [a, b, c, other];
    expect(inScope(plan, all, null).map((m) => m.id)).toEqual(['mig-c', 'mig-b', 'mig-a']);
    // wave_id is not the scope: vm-a is in wave-2's VMs
    expect(inScope(plan, all, w2).map((m) => m.id)).toEqual(['mig-a']);
    expect(queueByWave(plan, all).map((g) => [g.wave?.id ?? null, g.items.map((m) => m.id)])).toEqual([
      ['wave-1', ['mig-b']],
      ['wave-2', ['mig-a']],
      [null, ['mig-c']],
    ]);
    // a wave with nothing to finalize is left out
    expect(queueByWave({ ...plan, waves: [w1, wave('wave-3', 3, ['vm-z'])] }, [b]).map((g) => g.wave?.id)).toEqual(['wave-1']);
  });

  it('says what a bulk finalize did, and how many it skipped, never a cause the API did not give', () => {
    const skipped = (n: number) => Array.from({ length: n }, (_, i) => ({ migration_id: `mig-${i}`, reason: 'finalize failed: HTTP 409' }));
    expect(finalizedText({ finalized: ['mig-a'], skipped: [] })).toBe('1 VM finalized.');
    expect(finalizedText({ finalized: ['mig-a', 'mig-b'], skipped: skipped(1) })).toBe('2 VMs finalized. 1 not finalized: the page lists why.');
    expect(finalizedText({ finalized: [], skipped: skipped(2) })).toBe('0 VMs finalized. 2 not finalized: the page lists why.');
  });

  it('names each skipped VM with the reason the API gave (SDD §8.1)', () => {
    const m = completedAt('mig-a', '2026-10-09T00:00:00Z');
    const result = {
      finalized: [],
      skipped: [
        { migration_id: 'mig-a', reason: 'finalize failed: [Errno 28] No space left on device' },
        { migration_id: 'mig-gone', reason: 'only completed migrations can be finalized (rolled_back)' },
      ],
    };
    expect(skippedVms(result, [m])).toEqual([
      { id: 'mig-a', name: m.vm.name, reason: 'finalize failed: [Errno 28] No space left on device' },
      // a migration the page no longer lists keeps its id
      { id: 'mig-gone', name: 'mig-gone', reason: 'only completed migrations can be finalized (rolled_back)' },
    ]);
  });

  it('lists the completed migrations oldest first', () => {
    const young = completedAt('mig-young', '2026-10-10T10:00:00Z');
    const old = completedAt('mig-old', '2026-10-08T10:00:00Z');
    const done = { ...base, id: 'mig-done', phase: 'finalized' as const };
    expect(finalizeQueue([young, done, old]).map((m) => m.id)).toEqual(['mig-old', 'mig-young']);
  });
});
