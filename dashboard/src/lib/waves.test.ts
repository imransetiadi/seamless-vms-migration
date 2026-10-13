import { describe, expect, it } from 'vitest';
import type { Phase } from '../api/types';
import { waveRolledBack } from './waves';

const members = (...phases: Phase[]) => phases.map((phase) => ({ phase }));

describe('waveRolledBack (SDD §16)', () => {
  it('is a wave whose migrations it did not cancel all rolled back: it ended and migrated nothing', () => {
    expect(waveRolledBack(members('rolled_back', 'rolled_back'))).toBe(true);
    expect(waveRolledBack(members('rolled_back', 'cancelled'))).toBe(true);
  });

  it('is no wave that migrated a VM, nor one still at work, nor an empty or all-cancelled one', () => {
    expect(waveRolledBack(members('rolled_back', 'completed'))).toBe(false);
    expect(waveRolledBack(members('rolled_back', 'finalized'))).toBe(false);
    expect(waveRolledBack(members('rolled_back', 'cutover'))).toBe(false);
    expect(waveRolledBack(members())).toBe(false);
    expect(waveRolledBack(members('cancelled'))).toBe(false);
  });
});
