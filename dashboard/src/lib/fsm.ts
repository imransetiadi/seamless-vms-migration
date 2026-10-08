import type { Phase } from '../api/types';

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
