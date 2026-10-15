import { describe, expect, it } from 'vitest';
import { nothingMigrated, phaseCounts, planDone } from './planDone';

const plan = (status: 'draft' | 'validated' | 'running' | 'completed' | 'cancelled', vms = 3) => ({ status, vm_ids: Array.from({ length: vms }, (_, i) => `vm-${i}`) });

describe('planDone (SDD §16)', () => {
  it('counts completed and finalized over the migrations that are not cancelled, which are never done', () => {
    expect(planDone(plan('running'), { completed: 2, finalized: 1, syncing: 1 })).toEqual({ done: 3, total: 4, cancelled: 0 });
    // a VM taken out of the plan left a cancelled migration: the rest all finished, so the plan reads finished
    // and names it: a plan's VMs and its done total never differ without a word (SDD §16)
    expect(planDone(plan('completed', 4), { finalized: 3, cancelled: 1 })).toEqual({ done: 3, total: 3, cancelled: 1 });
  });

  it('counts the VMs before the first validation, and when every migration was cancelled', () => {
    expect(planDone(plan('draft'), {})).toEqual({ done: 0, total: 3, cancelled: 0 });
    // its VMs, not its cancelled migrations: nothing is claimed about them
    expect(planDone(plan('validated', 2), { cancelled: 2 })).toEqual({ done: 0, total: 2, cancelled: 0 });
  });

  it('gives a cancelled plan no figure: it has nothing left to do', () => {
    expect(planDone(plan('cancelled'), { cancelled: 2 })).toBeNull();
  });

  it('counts the phases of a list of migrations', () => {
    expect(phaseCounts([{ phase: 'completed' }, { phase: 'completed' }, { phase: 'cancelled' }])).toEqual({ completed: 2, cancelled: 1 });
  });
});

describe('nothingMigrated (SDD §16)', () => {
  const completed = (migration_counts: Record<string, number>) => ({ derived_status: 'completed' as const, migration_counts });

  it('is a Completed plan whose migrations it did not cancel all rolled back', () => {
    expect(nothingMigrated(completed({ rolled_back: 2 }))).toBe(true);
    expect(nothingMigrated(completed({ rolled_back: 1, cancelled: 3 }))).toBe(true);
  });

  it('is no plan that migrated a VM, nor one still at work, nor one with no migration', () => {
    expect(nothingMigrated(completed({ rolled_back: 1, completed: 1 }))).toBe(false);
    expect(nothingMigrated(completed({ rolled_back: 1, finalized: 1 }))).toBe(false);
    expect(nothingMigrated({ derived_status: 'running', migration_counts: { rolled_back: 2 } })).toBe(false);
    expect(nothingMigrated(completed({}))).toBe(false);
    expect(nothingMigrated(completed({ cancelled: 2 }))).toBe(false);
  });
});
