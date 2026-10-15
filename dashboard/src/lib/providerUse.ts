/** Which plans still need a provider, as the API decides it before a delete (SDD §12, §16). */
import type { Plan } from '../api/types';
import { planStatusMeta } from './status';

/** Phases that release a VM (SDD §5.4): a plan needs its providers for no such migration any more. */
const RELEASES_VM: ReadonlySet<string> = new Set(['cancelled', 'finalized', 'rolled_back']);

export interface ProviderUser {
  plan: Plan;
  /** Why it still needs the provider: its status as the plan shows it, or its VMs not finalized. */
  why: string;
}

const notFinalized = (n: number) => `${n} VM${n === 1 ? '' : 's'} not finalized`;

/**
 * The plans that still need a provider, as source or destination (SDD §12): one stored neither completed nor
 * cancelled, or a completed one with VMs not finalized, rolled back or cancelled - a rollback needs both providers
 * until a VM is finalized. The stored status decides, as in the API; the reason reads as the plan shows it. A cancelled
 * migration's cleanup still under way, and a rolled-back VM keeping its copied data for a cancel to clean up, keep the
 * providers too, but the plan list does not carry them: the API's 409 names them (SDD §16).
 */
export function plansThatNeed(providerId: string, plans: readonly Plan[]): ProviderUser[] {
  return plans.flatMap((plan) => {
    if (plan.source_provider_id !== providerId && plan.destination_provider_id !== providerId) return [];
    const holding = Object.entries(plan.migration_counts).reduce((n, [phase, count]) => (RELEASES_VM.has(phase) ? n : n + (count ?? 0)), 0);
    if (plan.status === 'completed' || plan.status === 'cancelled') return holding > 0 ? [{ plan, why: notFinalized(holding) }] : [];
    const why = plan.derived_status === 'completed' && holding > 0 ? notFinalized(holding) : planStatusMeta(plan.derived_status).label.toLowerCase();
    return [{ plan, why }];
  });
}

/** Why Delete is unavailable while plans still need the provider, three by name at most (SDD §16); null when none does. */
export function deleteRefusal(users: readonly ProviderUser[], shown = 3): string | null {
  if (!users.length) return null;
  const names = users
    .slice(0, shown)
    // quoted: a plan's name may hold parentheses, dashes and commas
    .map((u) => `“${u.plan.name}” (${u.why})`)
    .join(', ');
  const more = users.length > shown ? ` and ${users.length - shown} more` : '';
  return `${users.length === 1 ? 'A plan still needs' : 'Plans still need'} it: ${names}${more}. A plan needs its providers until it completes and its VMs are finalized.`;
}
