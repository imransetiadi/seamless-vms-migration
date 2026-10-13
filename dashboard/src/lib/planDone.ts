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
