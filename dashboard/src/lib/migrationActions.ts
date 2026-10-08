import type { Migration, Phase, Plan, Role } from '../api/types';
import { canTransition } from './fsm';
import { isWarmStrategy } from './phase';
import type { Availability } from './planActions';
import { hasRole } from './roles';

export type MigrationActionKey = 'approve' | 'cutover' | 'sync' | 'rollback' | 'retry' | 'cancel' | 'finalize';

export const MIGRATION_ACTION_ORDER: MigrationActionKey[] = ['approve', 'cutover', 'sync', 'retry', 'rollback', 'cancel', 'finalize'];

/** Minimum roles per SDD §12. */
export const ACTION_MIN_ROLE: Record<MigrationActionKey, Role> = {
  approve: 'approver',
  cutover: 'approver',
  sync: 'operator',
  rollback: 'operator',
  retry: 'operator',
  cancel: 'operator',
  finalize: 'approver',
};

const PRE_CUTOVER: ReadonlySet<Phase> = new Set(['ready', 'precopy', 'syncing', 'awaiting_cutover']);

function isSingleShot(m: Migration): boolean {
  return !isWarmStrategy(m.strategy);
}

function phaseRule(m: Migration, key: MigrationActionKey): Availability {
  const ok: Availability = { enabled: true, reason: null };
  const no = (reason: string): Availability => ({ enabled: false, reason });
  switch (key) {
    case 'approve':
      return PRE_CUTOVER.has(m.phase) ? ok : no('Approval applies before cutover: ready, pre-copy, syncing or awaiting cutover.');
    case 'cutover': {
      const gate = m.phase === 'awaiting_cutover' || (m.phase === 'ready' && isSingleShot(m));
      if (!gate) {
        if (!isSingleShot(m) && (m.phase === 'ready' || m.phase === 'precopy' || m.phase === 'syncing')) {
          return no('Warm migrations cut over once pre-copy has converged (awaiting cutover).');
        }
        return no('Cutover starts from awaiting cutover (warm) or ready (single-shot strategies).');
      }
      return m.cutover_requested ? no('Cutover already requested — waiting for the gate (window, concurrency).') : ok;
    }
    case 'sync':
      return m.phase === 'awaiting_cutover' ? ok : no('A delta sync can be requested only while awaiting cutover.');
    case 'rollback':
      if (m.phase === 'finalized') return no('A finalized migration cannot be rolled back.');
      return canTransition(m.phase, 'rolling_back')
        ? ok
        : no('Rollback is possible during cutover or verification, after a failure, or after completion until finalized.');
    case 'retry':
      return m.phase === 'failed' || m.phase === 'rolled_back' ? ok : no('Retry is available after a failure or a rollback.');
    case 'cancel':
      if (m.phase === 'failed' && m.downtime_started_at) return no('The source VM was stopped — roll back instead of cancelling.');
      return canTransition(m.phase, 'cancelled') ? ok : no('Cancel is not possible once cutover has started or the migration has ended.');
    case 'finalize':
      if (m.phase === 'finalized') return no('Already finalized.');
      return m.phase === 'completed' ? ok : no('Finalize becomes available once the migration is completed and verified.');
  }
}

/**
 * Which migration actions are valid now (SDD §5.1 transitions, §5.4 cutover gate, §12 roles).
 * Role gating wins so the reason names the missing role.
 */
export function migrationActions(m: Migration, role: Role | undefined): Record<MigrationActionKey, Availability> {
  const result = {} as Record<MigrationActionKey, Availability>;
  for (const key of MIGRATION_ACTION_ORDER) {
    const min = ACTION_MIN_ROLE[key];
    result[key] = hasRole(role, min) ? phaseRule(m, key) : { enabled: false, reason: `Requires the ${min} role.` };
  }
  return result;
}

export interface NextStep {
  text: string;
  action: MigrationActionKey | null;
}

/** One sentence that tells the operator what happens next, and the action that moves it on. */
export function nextStep(m: Migration, plan: Plan | null | undefined): NextStep {
  switch (m.phase) {
    case 'pending':
      return { text: 'Waiting for plan validation.', action: null };
    case 'validating':
      return { text: 'Validating: pre-flight checks and downtime estimates are running.', action: null };
    case 'blocked':
      return { text: 'Blocked. Next: fix the blocker findings, then re-validate the plan (operator).', action: null };
    case 'ready':
      if (isSingleShot(m)) {
        return m.cutover_requested
          ? { text: 'Cutover requested — waiting for the wave, the window or a free cutover slot.', action: null }
          : {
              text: `Ready. Next: an approver starts the cutover; the source VM stops for about ${Math.round((m.estimate?.downtime_s ?? 0) / 60)} min.`,
              action: 'cutover',
            };
      }
      return { text: 'Ready. Pre-copy starts when its wave runs; the source VM keeps running.', action: null };
    case 'precopy':
      return { text: 'Pre-copy is running while the source VM keeps running.', action: null };
    case 'syncing':
      return { text: 'Delta passes run until the change rate converges.', action: null };
    case 'awaiting_cutover':
      if (m.cutover_requested) return { text: 'Cutover requested — waiting for the window or a free cutover slot.', action: null };
      return {
        text:
          plan?.require_approval === false
            ? 'Converged. Next: request the cutover (approver); keep-warm syncs run meanwhile.'
            : 'Converged. Next: an approver approves and starts the cutover; keep-warm syncs run meanwhile.',
        action: 'cutover',
      };
    case 'cutover':
      return { text: 'Cutover in progress — the source VM is stopped and the downtime clock is running.', action: null };
    case 'verifying':
      return { text: 'Verifying the destination VM: server state, ports, TCP probes and console.', action: null };
    case 'completed':
      return { text: 'Verified on RHOSO. Next: finalize to clean up the source (approver), or roll back if needed.', action: 'finalize' };
    case 'finalized':
      return { text: 'Finalized. The migration is done.', action: null };
    case 'failed':
      return m.downtime_started_at && !m.downtime_ended_at
        ? { text: 'Failed after the source VM was stopped. Next: roll back to restart it, or fix the cause and retry.', action: 'rollback' }
        : { text: 'Failed. Next: fix the cause and retry, or cancel.', action: 'retry' };
    case 'rolling_back':
      return { text: 'Rolling back: removing the destination VM and restarting the source.', action: null };
    case 'rolled_back':
      return { text: 'Rolled back; the source VM runs again. Next: fix the cause and retry.', action: 'retry' };
    case 'cancelled':
      return { text: 'Cancelled. Nothing further will run.', action: null };
  }
}
