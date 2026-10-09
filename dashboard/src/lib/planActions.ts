import type { Migration, Plan, Role } from '../api/types';
import { hasRole } from './roles';

export type PlanActionKey = 'validate' | 'waves' | 'start' | 'pause' | 'edit';

export interface Availability {
  enabled: boolean;
  /** Why the action is unavailable (shown as tooltip and to screen readers). */
  reason: string | null;
}

const ROLE_REASON = 'Requires the operator role.';

/**
 * Plan actions (SDD §12: all need `operator`). Status rules: PATCH-like actions (auto waves) only in
 * draft/validated; validate anything that is not running or completed; start validated or paused
 * plans (the API answers 409 while a migration is blocked); pause running plans.
 */
export function planActions(plan: Plan, role: Role | undefined, migrations: Migration[]): Record<PlanActionKey, Availability> {
  const allowed = hasRole(role, 'operator');
  const gate = (ok: boolean, reason: string): Availability => {
    if (!allowed) return { enabled: false, reason: ROLE_REASON };
    return ok ? { enabled: true, reason: null } : { enabled: false, reason };
  };
  const status = plan.status;
  const blocked = migrations.filter((m) => m.plan_id === plan.id && m.phase === 'blocked').length;

  const start = (): Availability => {
    switch (status) {
      case 'draft':
        return gate(false, 'Validate the plan first.');
      case 'running':
        return gate(false, 'The plan is already running.');
      case 'completed':
        return gate(false, 'The plan has completed.');
      case 'failed':
        return gate(false, 'Retry, roll back or cancel the failed migrations first.');
      default:
        if (blocked > 0) {
          return gate(false, `${blocked} migration${blocked === 1 ? ' is' : 's are'} blocked — fix the findings and re-validate.`);
        }
        return gate(true, '');
    }
  };

  return {
    validate: gate(
      status !== 'running' && status !== 'completed',
      status === 'running' ? 'Pause the plan before re-validating it.' : 'A completed plan cannot be re-validated.',
    ),
    waves: gate(status === 'draft' || status === 'validated', 'Waves can only be planned while the plan is draft or validated.'),
    start: start(),
    pause: gate(status === 'running', 'Only a running plan can be paused.'),
    // PATCH /plans/{id} (SDD §12): only in draft or validated; saving returns the plan to draft
    edit: gate(status === 'draft' || status === 'validated', `Only a draft or validated plan can be edited (this one is ${status}).`),
  };
}
