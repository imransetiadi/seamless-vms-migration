import type { MigrationLike, Plan, Role } from '../api/types';
import { bulkFinalize, everySourceRunsText } from './finalize';
import { blocksPlanChange, releaseGroups } from './inFlight';
import { hasRole } from './roles';
import { WAVE_COMPLETE_PHASES } from './waves';

export type PlanActionKey = 'validate' | 'waves' | 'start' | 'pause' | 'edit' | 'finalize' | 'cancel';

export interface Availability {
  enabled: boolean;
  /** Why the action is unavailable (shown as tooltip and to screen readers). */
  reason: string | null;
}

const ROLE_REASON = 'Requires the operator role.';
/** A migration that never started (SDD §12): Cancel plan cancels these; a cancelled one never counts as started. */
const UNSTARTED = new Set(['pending', 'blocked', 'ready', 'cancelled']);

/** Up to three names, then how many more. */
function names(list: string[]): string {
  return `${list.slice(0, 3).join(', ')}${list.length > 3 ? ` and ${list.length - 3} more` : ''}`;
}

/**
 * Plan actions (SDD §12: all need `operator`). Status rules: PATCH-like actions (auto waves) only in
 * draft/validated; validate anything that is not running or completed; start validated or paused
 * plans (the API answers 409 while a migration is blocked or, in a plan with waves, belongs to no wave);
 * pause running plans.
 */
export function planActions(plan: Plan, role: Role | undefined, migrations: readonly MigrationLike[]): Record<PlanActionKey, Availability> {
  const finalizeGate = (): Availability => {
    if (!hasRole(role, 'approver')) return { enabled: false, reason: 'Requires the approver role.' };
    const queue = migrations.filter((m) => m.plan_id === plan.id && m.phase === 'completed');
    if (queue.length === 0) return { enabled: false, reason: 'No VM awaits finalize.' };
    return bulkFinalize(queue).finalizes.length > 0 ? { enabled: true, reason: null } : { enabled: false, reason: everySourceRunsText(queue) };
  };
  const allowed = hasRole(role, 'operator');
  const gate = (ok: boolean, reason: string): Availability => {
    if (!allowed) return { enabled: false, reason: ROLE_REASON };
    return ok ? { enabled: true, reason: null } : { enabled: false, reason };
  };
  const status = plan.status;
  // Start and Resume name the VMs that hold them back, as the API's 409 does (SDD §16)
  const blockedVms = migrations.filter((m) => m.plan_id === plan.id && m.phase === 'blocked').map((m) => m.vm.name);
  const blocked = blockedVms.length;
  // SDD §12: a migration in flight depends on the plan as it was — no edit, no re-planned waves meanwhile;
  // each VM is named with what releases it (a completed one is finalized or rolled back, never cancelled)
  const groups = releaseGroups(migrations.filter((m) => m.plan_id === plan.id && blocksPlanChange(m)));
  const busy = groups.length
    ? groups.map((g) => `${g.action[0]!.toUpperCase()}${g.action.slice(1)} ${g.what} first: ${names(g.names)}.`).join(' ')
    : null;
  // SDD §9.4: start_plan refuses a plan with waves while a migration that has not finished belongs to
  // no wave — a VM added after the waves were planned would start outside every wave's order
  const waveIds = new Set(plan.waves.map((w) => w.id));
  const stray = plan.waves.length
    ? migrations
        .filter((m) => m.plan_id === plan.id && !WAVE_COMPLETE_PHASES.has(m.phase) && !(m.wave_id && waveIds.has(m.wave_id)))
        .map((m) => m.vm.name)
    : [];

  // POST /plans/{id}/cancel (SDD §12) names each VM whose migration started, with its phase
  const startedVms = migrations.filter((m) => m.plan_id === plan.id && !UNSTARTED.has(m.phase)).map((m) => `${m.vm.name} (${m.phase})`);
  const cancel = (): Availability => {
    if (status === 'cancelled') return gate(false, 'The plan is cancelled.');
    if (status === 'running') return gate(false, 'Pause the plan before cancelling it.');
    if (startedVms.length) {
      return gate(
        false,
        `Only a plan whose migrations have not started can be cancelled: ${names(startedVms)}. Cancel a failed or rolled-back migration on its own first.`,
      );
    }
    return gate(true, '');
  };

  const start = (): Availability => {
    // a plan whose migrations all ended is finished, whatever a re-validation stored (SDD §8.1, ADV-19)
    if (plan.derived_status === 'completed') return gate(false, 'The plan has completed: finalize or roll back its VMs.');
    if (plan.derived_status === 'finalized') return gate(false, 'The plan is finalized.');
    switch (status) {
      case 'draft':
        return gate(false, 'Validate the plan first.');
      case 'running':
        return gate(false, 'The plan is already running.');
      case 'completed':
        return gate(false, 'The plan has completed.');
      case 'cancelled':
        return gate(false, 'The plan is cancelled.');
      // 'failed': pre-staging failed; Start pre-stages the plan again (SDD §8)
      default:
        if (blocked > 0) {
          return gate(false, `${blocked} migration${blocked === 1 ? ' is' : 's are'} blocked: ${names(blockedVms)} — fix the findings and re-validate.`);
        }
        if (stray.length > 0) {
          const one = stray.length === 1;
          return gate(
            false,
            `${stray.length} migration${one ? ' belongs' : 's belong'} to no wave: ${names(stray)} — plan the waves again or add ${one ? 'it' : 'them'} to a wave.`,
          );
        }
        return gate(true, '');
    }
  };

  return {
    validate: gate(
      status !== 'running' && status !== 'completed' && status !== 'cancelled',
      status === 'running'
        ? 'Pause the plan before re-validating it.'
        : status === 'cancelled'
          ? 'A cancelled plan cannot be validated.'
          : 'A completed plan cannot be re-validated.',
    ),
    // like the API (SDD §12): draft, validated or paused; the plan then returns to draft
    waves: gate(
      (status === 'draft' || status === 'validated' || status === 'paused') && !busy,
      status === 'draft' || status === 'validated' || status === 'paused'
        ? (busy ?? '')
        : 'Waves can only be planned while the plan is draft, validated or paused.',
    ),
    start: start(),
    pause: gate(status === 'running', 'Only a running plan can be paused.'),
    // POST /plans/{id}/finalize (SDD §8.1): an approver, while a VM awaits finalize whose source VM is stopped - it skips
    // one whose source runs again
    finalize: finalizeGate(),
    // PATCH /plans/{id} (SDD §12): only in draft or validated; saving returns the plan to draft
    edit: gate(
      (status === 'draft' || status === 'validated') && !busy,
      status === 'draft' || status === 'validated' ? (busy ?? '') : `Only a draft or validated plan can be edited (this one is ${status}).`,
    ),
    cancel: cancel(),
  };
}
