import type { Approval, Migration, MigrationLike, Phase, Plan, Role } from '../api/types';
import { completedSince, isOverdue } from './finalize';
import { formatRelative } from './format';
import { canTransition } from './fsm';
import { isWarmStrategy } from './phase';
import type { Availability } from './planActions';
import { hasRole, OWN_CUTOVER, OWN_FORCE, PERMISSIONS } from './roles';

export type MigrationActionKey = 'approve' | 'cutover' | 'force-cutover' | 'sync' | 'rollback' | 'retry' | 'cancel' | 'finalize';

export const MIGRATION_ACTION_ORDER: MigrationActionKey[] = ['approve', 'cutover', 'force-cutover', 'sync', 'retry', 'rollback', 'cancel', 'finalize'];

/** Minimum roles per SDD §12. */
export const ACTION_MIN_ROLE: Record<MigrationActionKey, Role> = {
  approve: 'approver',
  cutover: 'approver',
  'force-cutover': 'approver',
  sync: 'operator',
  rollback: 'operator',
  retry: 'operator',
  cancel: 'operator',
  finalize: 'approver',
};

/** Where an approval lasts (SDD §16): the API also takes one before validation, which clears it. */
const APPROVAL_LASTS: ReadonlySet<Phase> = new Set(['ready', 'precopy', 'syncing', 'awaiting_cutover', 'failed', 'rolled_back']);
const BEFORE_VALIDATION: ReadonlySet<Phase> = new Set(['pending', 'validating', 'blocked']);
/** The API's CUTOVER_REQUESTABLE (SDD §5.4): a warm migration requested early cuts over once it converges. */
const CUTOVER_REQUESTABLE: ReadonlySet<Phase> = new Set(['ready', 'precopy', 'syncing', 'awaiting_cutover']);
/** The API's FORCEABLE (SDD §5.4, ADV-16): a Force cutover skips the passes still to come. */
const FORCEABLE: ReadonlySet<Phase> = new Set(['precopy', 'syncing']);

/** The downtime clock is open: the source VM was stopped and has not run since (SDD §5.2). */
function sourceStopped(m: Migration): boolean {
  return Boolean(m.downtime_started_at) && !m.downtime_ended_at;
}

function isSingleShot(m: Migration): boolean {
  return !isWarmStrategy(m.strategy);
}

/**
 * The approvals the cutover gate counts (SDD §5.4 rule 1): all but, when the plan requires a distinct
 * approver, the requester's and the forcer's.
 */
export function countedApprovals(m: Pick<Migration, 'approvals' | 'cutover_requested_by' | 'force_cutover'>, plan: Plan | null | undefined): Approval[] {
  // while the plan loads, the API's default holds: a distinct approver
  if (plan?.require_distinct_approver === false) return m.approvals;
  const own = new Set([m.cutover_requested_by, m.force_cutover?.actor].filter(Boolean));
  return own.size ? m.approvals.filter((a) => !own.has(a.actor)) : m.approvals;
}

/** Why `actor`'s approval does not count (SDD §5.4, §16): they forced the cutover, or requested it. */
export function uncountedReason(m: Migration, actor: string): string {
  return m.force_cutover?.actor === actor ? 'forced the cutover' : 'requested the cutover';
}

/** Maker-checker (ADV-17): the plan wants an approval, and not from the cutover's requester. */
function fourEyes(plan: Plan | null | undefined): boolean {
  return Boolean(plan?.require_approval && plan.require_distinct_approver);
}

/** A requested cutover that waits for a closed window: an approver may let it cut over outside the window (SDD §5.4). */
export function waitsOnClosedWindow(m: Migration, plan: Plan | null | undefined, now: number): boolean {
  const w = plan?.cutover_window;
  if (!m.cutover_requested || m.force_window || !w) return false;
  return now < Date.parse(w.start) || now > Date.parse(w.end);
}

function phaseRule(m: Migration, key: MigrationActionKey, plan: Plan | null | undefined, now: number, user: string | undefined): Availability {
  const ok: Availability = { enabled: true, reason: null };
  const no = (reason: string): Availability => ({ enabled: false, reason });
  switch (key) {
    case 'approve':
      if (APPROVAL_LASTS.has(m.phase)) {
        // maker-checker (SDD §5.4): the API refuses the requester's approval, and the forcer's
        if (user && m.cutover_requested_by === user && plan?.require_distinct_approver !== false) return no(OWN_CUTOVER);
        if (user && m.force_cutover?.actor === user && plan?.require_distinct_approver !== false) return no(OWN_FORCE);
        return ok;
      }
      if (BEFORE_VALIDATION.has(m.phase)) return no('Validate the plan first: validation clears approvals, so approve its new assessment.');
      if (m.phase === 'cancelled') return no('A cancelled migration takes no approval.');
      return no('The cutover has started or ended: an approval no longer applies.');
    case 'cutover': {
      if (!CUTOVER_REQUESTABLE.has(m.phase)) return no('A cutover can be requested from ready until it starts.');
      if (!m.cutover_requested || waitsOnClosedWindow(m, plan, now)) return ok;
      return no(
        m.phase === 'awaiting_cutover' || isSingleShot(m)
          ? 'Cutover already requested — waiting for the gate (window, concurrency).'
          : 'Cutover already requested — it starts once pre-copy has converged and the gate opens.',
      );
    }
    case 'force-cutover':
      if (isSingleShot(m)) return no('Only a warm migration has passes to skip.');
      if (!FORCEABLE.has(m.phase)) return no('A Force cutover skips convergence: it applies while pre-copy or delta passes run.');
      if (m.force_cutover) return no(`Already forced by ${m.force_cutover.actor}: the cutover follows the running pass.`);
      return ok;
    case 'sync':
      return m.phase === 'awaiting_cutover' ? ok : no('A delta sync can be requested only while awaiting cutover.');
    case 'rollback':
      if (m.phase === 'finalized') return no('A finalized migration cannot be rolled back.');
      // SDD §8.1: the source may be gone; a rollback would delete the destination server and find no source
      if (m.phase === 'completed' && m.checkpoint === 'finalizing') return no('The finalize began deleting the source VM: finalize it again.');
      return canTransition(m.phase, 'rolling_back')
        ? ok
        : no('Rollback is possible during cutover or verification, after a failure, or after completion until finalized.');
    case 'retry':
      return m.phase === 'failed' || m.phase === 'rolled_back' ? ok : no('Retry is available after a failure or a rollback.');
    case 'cancel':
      // SDD §5.1: a validation may be moving it; a validation finishes it, or cancels a leftover whose VM left the plan
      if (m.phase === 'validating') return no('Being validated: validate the plan again — it finishes this migration, or cancels it once its VM left the plan.');
      // SDD §5.1: a cancel would leave a stopped source with nothing left to restart it (only where the
      // phase could be cancelled at all: a cutover in progress has its clock open by design)
      if (canTransition(m.phase, 'cancelled') && sourceStopped(m)) {
        return no(
          m.phase === 'failed'
            ? 'The source VM is stopped — roll back to restart it, or retry the cutover.'
            : 'The source VM is stopped after a failed cutover — cut it over; it cannot be cancelled until it runs again.',
        );
      }
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
export function migrationActions(
  m: Migration,
  role: Role | undefined,
  plan?: Plan | null,
  now: number = Date.now(),
  /** The signed-in user: the cutover's requester does not approve it (SDD §5.4). */
  user?: string,
): Record<MigrationActionKey, Availability> {
  const result = {} as Record<MigrationActionKey, Availability>;
  for (const key of MIGRATION_ACTION_ORDER) {
    const min = ACTION_MIN_ROLE[key];
    result[key] = hasRole(role, min) ? phaseRule(m, key, plan, now, user) : { enabled: false, reason: `Requires the ${min} role.` };
  }
  return result;
}

export interface NextStep {
  text: string;
  action: MigrationActionKey | null;
}

/** One sentence that tells the operator what happens next, and the action that moves it on. */
export function nextStep(m: Migration, plan: Plan | null | undefined, now: number = Date.now()): NextStep {
  switch (m.phase) {
    case 'pending':
      return { text: 'Waiting for plan validation.', action: null };
    case 'validating':
      return { text: 'Validating: pre-flight checks and downtime estimates are running.', action: null };
    case 'blocked':
      return { text: 'Blocked. Next: fix the blocker findings, then re-validate the plan (operator).', action: null };
    case 'ready':
      if (m.advisor_hold) {
        // neither its pre-copy nor its cutover starts before a person decides (SDD §5.4, §14.6)
        return {
          text: `Ready. Next: approve or override the advisor's decision (${PERMISSIONS['advisor.approve']}); nothing starts before that.`,
          action: null,
        };
      }
      if (sourceStopped(m)) {
        // a retried cutover (SDD §5.2): the source has been down since the failed attempt
        if (!isSingleShot(m)) {
          return { text: 'Retried: the source VM is still stopped and its downtime clock keeps running; pre-copy starts when its wave runs, then the cutover.', action: null };
        }
        return m.cutover_requested
          ? { text: 'Cutover requested again — the source VM is still stopped and its downtime clock keeps running.', action: null }
          : { text: 'Retried: the source VM is still stopped and its downtime clock keeps running. Next: an approver starts the cutover again.', action: 'cutover' };
      }
      if (isSingleShot(m)) {
        const stops = `the source VM stops for about ${Math.round((m.estimate?.downtime_s ?? 0) / 60)} min`;
        if (m.cutover_requested && m.cutover_requested_by && fourEyes(plan) && countedApprovals(m, plan).length === 0) {
          return { text: `Cutover requested by ${m.cutover_requested_by}. Next: another approver approves it; ${stops}.`, action: 'approve' };
        }
        return m.cutover_requested
          ? { text: 'Cutover requested — waiting for the wave, the window or a free cutover slot.', action: null }
          : {
              text: fourEyes(plan) ? `Ready. Next: an approver requests the cutover and another approves it; ${stops}.` : `Ready. Next: an approver starts the cutover; ${stops}.`,
              action: 'cutover',
            };
      }
      return { text: 'Ready. Pre-copy starts when its wave runs; the source VM keeps running.', action: null };
    case 'precopy':
    case 'syncing':
      if (m.force_cutover) {
        // a Force cutover is a request: the gate still applies, maker-checker too (SDD §5.4, ADV-16)
        const by = m.force_cutover.actor;
        if (fourEyes(plan) && countedApprovals(m, plan).length === 0) {
          return { text: `Cutover forced by ${by}. Next: another approver approves it; it starts after the running pass.`, action: 'approve' };
        }
        return { text: `Cutover forced by ${by}: it starts after the running pass, once the window and a free cutover slot allow.`, action: null };
      }
      return m.phase === 'precopy'
        ? { text: 'Pre-copy is running while the source VM keeps running.', action: null }
        : { text: 'Delta passes run until the change rate converges.', action: null };
    case 'awaiting_cutover':
      if (m.cutover_refusals.length > 0) {
        // SDD §16: the next step agrees with the warning - requested or not, the cutover would refuse them
        return {
          text: 'Converged, but the cutover will refuse what the last pass found. Next: fix it, then sync so a pass looks again (operator); keep-warm syncs look too.',
          action: 'sync',
        };
      }
      // a request stored before ADV-17 names nobody: the wait is the gate's, as before
      if (m.cutover_requested && m.cutover_requested_by && fourEyes(plan) && countedApprovals(m, plan).length === 0) {
        return { text: `Cutover requested by ${m.cutover_requested_by}. Next: another approver approves it; keep-warm syncs run meanwhile.`, action: 'approve' };
      }
      if (waitsOnClosedWindow(m, plan, now)) {
        return { text: 'Cutover requested — waiting for the cutover window; an approver can let it cut over outside the window.', action: 'cutover' };
      }
      if (m.cutover_requested) return { text: 'Cutover requested — waiting for the window or a free cutover slot.', action: null };
      return {
        text:
          plan?.require_approval === false
            ? 'Converged. Next: request the cutover (approver); keep-warm syncs run meanwhile.'
            : fourEyes(plan)
              ? 'Converged. Next: an approver requests the cutover and another approves it; keep-warm syncs run meanwhile.'
              : 'Converged. Next: an approver approves and starts the cutover; keep-warm syncs run meanwhile.',
        action: 'cutover',
      };
    case 'cutover':
      return { text: 'Cutover in progress — the source VM is stopped and the downtime clock is running.', action: null };
    case 'verifying':
      return { text: 'Verifying the destination VM: server state, ports, TCP probes and console.', action: null };
    case 'completed': {
      if (m.source_running_at) {
        // the source watch saw it running (SDD §8.1): finalizing now would keep a diverged source
        return {
          text: 'The source VM runs again, so its data diverges from RHOSO. Next: stop it in the source cloud, or roll it back; finalize once it is stopped.',
          action: null,
        };
      }
      // its time in the finalize queue, flagged past the plan's reminder (SDD §8.1, ADV-19)
      const since = completedSince(m);
      const verified = `Verified on RHOSO${since ? ` ${formatRelative(since, now)}` : ''}.`;
      if (plan && isOverdue(m, plan.finalize_warning_h, now)) {
        return { text: `${verified} It has waited in Completed longer than the plan allows (${plan.finalize_warning_h} h): finalize it or roll it back.`, action: 'finalize' };
      }
      return { text: `${verified} Next: finalize to clean up the source (approver), or roll back if needed.`, action: 'finalize' };
    }
    case 'finalized':
      return { text: 'Finalized. The migration is done.', action: null };
    case 'failed':
      if (m.downtime_started_at && !m.downtime_ended_at) {
        return { text: 'Failed after the source VM was stopped. Next: roll back to restart it, or fix the cause and retry.', action: 'rollback' };
      }
      if (m.cutover_refusals.length > 0 && m.error?.startsWith('cutover: ')) {
        // SDD §16: the cutover's check refused what the passes found, before the source stopped
        return {
          text: "Failed: the cutover refused what the passes found, before the source stopped. Next: fix it, then retry — the retry's pass looks again — or cancel.",
          action: 'retry',
        };
      }
      return { text: 'Failed. Next: fix the cause and retry, or cancel.', action: 'retry' };
    case 'rolling_back':
      return { text: 'Rolling back: removing the destination VM and restarting the source.', action: null };
    case 'rolled_back':
      return { text: 'Rolled back; the source VM runs again. Next: fix the cause and retry.', action: 'retry' };
    case 'cancelled':
      return { text: 'Cancelled. Nothing further will run.', action: null };
  }
}

/** Phases a re-validation or a strategy change touches; it clears their approvals and cutover requests (SDD §5.4). */
export const REVALIDATABLE: ReadonlySet<Phase> = new Set<Phase>(['pending', 'blocked', 'ready']);

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

/** What validating the plan again would clear, e.g. "2 approvals and 1 cutover request"; null when nothing (SDD §5.4, §16). */
export function clearedByValidation(migrations: readonly MigrationLike[]): string | null {
  const touched = migrations.filter((m) => REVALIDATABLE.has(m.phase));
  const approvals = touched.reduce((n, m) => n + m.approvals.length, 0);
  const requests = touched.filter((m) => m.cutover_requested).length;
  const parts = [approvals ? count(approvals, 'approval') : '', requests ? count(requests, 'cutover request') : ''].filter(Boolean);
  return parts.length ? parts.join(' and ') : null;
}

/** What choosing another strategy would clear, e.g. "1 approval and the cutover request"; null when nothing (SDD §5.4, §16). */
export function clearedByStrategyChange(m: Migration): string | null {
  const parts = [m.approvals.length ? count(m.approvals.length, 'approval') : '', m.cutover_requested ? 'the cutover request' : ''].filter(Boolean);
  return parts.length ? parts.join(' and ') : null;
}
