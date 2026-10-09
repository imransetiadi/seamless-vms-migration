import type { Migration, Phase, Plan } from '../api/types';

/** Phases in which a migration lets its VM go (SDD §5.4); in every other phase it holds the VM. */
export const RELEASES_VM: ReadonlySet<Phase> = new Set<Phase>(['cancelled', 'finalized', 'rolled_back']);

/**
 * The VMs of `vmIds` that migrations of other plans with the source provider `sourceId` hold (SDD §5.4),
 * by source id: `name (plan "…", phase)` once per holder, the API's wording. `planId` is the plan being
 * edited, whose own migrations do not count.
 */
export function heldElsewhere(
  vmIds: Iterable<string>,
  sourceId: string,
  planId: string | undefined,
  plans: readonly Plan[],
  migrations: readonly Migration[],
): Map<string, string[]> {
  const wanted = new Set(vmIds);
  const others = new Map(plans.filter((p) => p.id !== planId && p.source_provider_id === sourceId).map((p) => [p.id, p]));
  const held = new Map<string, Set<string>>();
  for (const m of migrations) {
    const other = others.get(m.plan_id);
    if (!other || !wanted.has(m.vm.source_id) || RELEASES_VM.has(m.phase)) continue;
    const holders = held.get(m.vm.source_id) ?? new Set<string>();
    holders.add(`${m.vm.name} (plan "${other.name}", ${m.phase})`);
    held.set(m.vm.source_id, holders);
  }
  return new Map([...held].sort(([a], [b]) => a.localeCompare(b)).map(([vmId, holders]) => [vmId, [...holders].sort()]));
}
