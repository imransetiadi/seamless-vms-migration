import type { Migration, Phase } from '../api/types';

/** Allowed migration phase transitions — SDD §5.1 (`seamless_migrate.domain.fsm.TRANSITIONS`). */
export const TRANSITIONS: Readonly<Record<Phase, readonly Phase[]>> = {
  pending: ['validating', 'cancelled'],
  validating: ['ready', 'blocked', 'failed'],
  blocked: ['validating', 'cancelled'],
  ready: ['precopy', 'cutover', 'validating', 'cancelled'],
  precopy: ['syncing', 'awaiting_cutover', 'failed', 'cancelled'],
  syncing: ['awaiting_cutover', 'failed', 'cancelled'],
  awaiting_cutover: ['syncing', 'cutover', 'cancelled'],
  cutover: ['verifying', 'failed', 'rolling_back'],
  verifying: ['completed', 'failed', 'rolling_back'],
  completed: ['finalized', 'rolling_back'],
  failed: ['rolling_back', 'ready', 'cancelled'],
  rolling_back: ['rolled_back', 'failed'],
  rolled_back: ['ready'],
  finalized: [],
  cancelled: [],
};

export function canTransition(from: Phase, to: Phase): boolean {
  return TRANSITIONS[from]?.includes(to) ?? false;
}

/**
 * Whether the migration has a data path that a cancel cleans up (SDD §5.1): it recorded passes, or a
 * pass runs (or waits for its retry) in precopy/syncing — its copied data would otherwise stay behind.
 */
export function hasDataPath(m: Pick<Migration, 'phase' | 'sync_passes'>): boolean {
  return m.sync_passes.length > 0 || m.phase === 'precopy' || m.phase === 'syncing';
}
