import type { Phase, Plan } from '../api/types';

export type PhaseCounts = Partial<Record<Phase, number>>;

/** How many migrations of a list are in each phase, as `Plan.migration_counts` carries them (SDD §4). */
export function phaseCounts(migrations: ReadonlyArray<{ phase: Phase }>): PhaseCounts {
  const counts: PhaseCounts = {};
  for (const m of migrations) counts[m.phase] = (counts[m.phase] ?? 0) + 1;
  return counts;
}

/**
 * A plan's migrations done (SDD §16): `completed` and `finalized` of those that are not `cancelled` — a cancelled
 * migration is never done (a VM taken out of the plan, §5.4) — or of its VMs before validation; null for a
 * cancelled plan, which has nothing left to do.
 */
export function planDone(plan: Pick<Plan, 'status' | 'vm_ids'>, counts: PhaseCounts): { done: number; total: number } | null {
  if (plan.status === 'cancelled') return null;
  const total = Object.entries(counts).reduce((sum, [phase, n]) => (phase === 'cancelled' ? sum : sum + (n ?? 0)), 0);
  if (!total) return { done: 0, total: plan.vm_ids.length };
  return { done: (counts.completed ?? 0) + (counts.finalized ?? 0), total };
}

/** Why a Completed plan that migrated nothing is no success, and what to do next (SDD §16). */
export const NOTHING_MIGRATED = 'Nothing was migrated: every migration rolled back.';
export const NOTHING_MIGRATED_NEXT =
  'Nothing was migrated: every migration rolled back and its source VM runs again. Retry each one from its page once the cause is fixed, or cancel it.';

/**
 * A Completed plan that migrated nothing: every migration it did not cancel rolled back (SDD §8.1 rule 6, §16). It
 * never reads as a success.
 */
export function nothingMigrated(plan: Pick<Plan, 'derived_status' | 'migration_counts'>): boolean {
  const counts = plan.migration_counts;
  return plan.derived_status === 'completed' && !counts.completed && !counts.finalized && (counts.rolled_back ?? 0) > 0;
}
