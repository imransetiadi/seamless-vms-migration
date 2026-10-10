import type { Migration, Phase, Plan, Role } from '../api/types';
import { hasRole } from './roles';
import { WAVE_COMPLETE_PHASES } from './waves';

export type PlanActionKey = 'validate' | 'waves' | 'start' | 'pause' | 'edit' | 'finalize';

export interface Availability {
  enabled: boolean;
  /** Why the action is unavailable (shown as tooltip and to screen readers). */
  reason: string | null;
}

const ROLE_REASON = 'Requires the operator role.';

/** Phases in which a migration depends on the plan's providers, mappings and strategy (SDD §12). */
const IN_FLIGHT: ReadonlySet<Phase> = new Set(['precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'rolling_back', 'completed']);

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
export function planActions(plan: Plan, role: Role | undefined, migrations: Migration[]): Record<PlanActionKey, Availability> {
  const allowed = hasRole(role, 'operator');
  const gate = (ok: boolean, reason: string): Availability => {
    if (!allowed) return { enabled: false, reason: ROLE_REASON };
    return ok ? { enabled: true, reason: null } : { enabled: false, reason };
  };
  const status = plan.status;
  const blocked = migrations.filter((m) => m.plan_id === plan.id && m.phase === 'blocked').length;
  // SDD §12: a migration in flight depends on the plan as it was — no edit, no re-planned waves meanwhile
  const inFlight = migrations
    .filter((m) => m.plan_id === plan.id && (IN_FLIGHT.has(m.phase) || (Boolean(m.downtime_started_at) && !m.downtime_ended_at)))
    .map((m) => m.vm.name);
  const busy = inFlight.length ? `Migrations in flight: ${names(inFlight)} — finish, roll back or cancel them first.` : null;
  // SDD §9.4: start_plan refuses a plan with waves while a migration that has not finished belongs to
  // no wave — a VM added after the waves were planned would start outside every wave's order
  const waveIds = new Set(plan.waves.map((w) => w.id));
  const stray = plan.waves.length
    ? migrations
        .filter((m) => m.plan_id === plan.id && !WAVE_COMPLETE_PHASES.has(m.phase) && !(m.wave_id && waveIds.has(m.wave_id)))
        .map((m) => m.vm.name)
    : [];

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
      // 'failed': pre-staging failed; Start pre-stages the plan again (SDD §8)
      default:
        if (blocked > 0) {
          return gate(false, `${blocked} migration${blocked === 1 ? ' is' : 's are'} blocked — fix the findings and re-validate.`);
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
      status !== 'running' && status !== 'completed',
      status === 'running' ? 'Pause the plan before re-validating it.' : 'A completed plan cannot be re-validated.',
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
    // POST /plans/{id}/finalize (SDD §8.1): an approver, while a VM awaits finalize
    finalize: !hasRole(role, 'approver')
      ? { enabled: false, reason: 'Requires the approver role.' }
      : migrations.some((m) => m.plan_id === plan.id && m.phase === 'completed')
        ? { enabled: true, reason: null }
        : { enabled: false, reason: 'No VM awaits finalize.' },
    // PATCH /plans/{id} (SDD §12): only in draft or validated; saving returns the plan to draft
    edit: gate(
      (status === 'draft' || status === 'validated') && !busy,
      status === 'draft' || status === 'validated' ? (busy ?? '') : `Only a draft or validated plan can be edited (this one is ${status}).`,
    ),
  };
}
