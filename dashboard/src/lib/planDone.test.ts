import { describe, expect, it } from 'vitest';
import { phaseCounts, planDone } from './planDone';

const plan = (status: 'draft' | 'validated' | 'running' | 'completed' | 'cancelled', vms = 3) => ({ status, vm_ids: Array.from({ length: vms }, (_, i) => `vm-${i}`) });

describe('planDone (SDD §16)', () => {
  it('counts completed and finalized over the migrations that are not cancelled, which are never done', () => {
    expect(planDone(plan('running'), { completed: 2, finalized: 1, syncing: 1 })).toEqual({ done: 3, total: 4 });
    // a VM taken out of the plan left a cancelled migration: the rest all finished, so the plan reads finished
    expect(planDone(plan('completed'), { finalized: 3, cancelled: 1 })).toEqual({ done: 3, total: 3 });
  });

  it('counts the VMs before the first validation, and when every migration was cancelled', () => {
    expect(planDone(plan('draft'), {})).toEqual({ done: 0, total: 3 });
    expect(planDone(plan('validated', 2), { cancelled: 2 })).toEqual({ done: 0, total: 2 });
  });

  it('gives a cancelled plan no figure: it has nothing left to do', () => {
    expect(planDone(plan('cancelled'), { cancelled: 2 })).toBeNull();
  });

  it('counts the phases of a list of migrations', () => {
    expect(phaseCounts([{ phase: 'completed' }, { phase: 'completed' }, { phase: 'cancelled' }])).toEqual({ completed: 2, cancelled: 1 });
  });
});
