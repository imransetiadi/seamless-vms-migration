import type { Migration, Phase, Plan } from '../api/types';

/** Phases in which a migration lets its VM go (SDD §5.4); in every other phase it holds the VM. */
export const RELEASES_VM: ReadonlySet<Phase> = new Set<Phase>(['cancelled', 'finalized', 'rolled_back']);

/**
 * The VMs of `vmIds` that migrations of other plans with the source provider `sourceId` hold (SDD §5.4),
 * by source id: `name (plan "…", plan-id, phase)` once per holding plan, the API's wording (plan names need not
 * be unique: the id tells two plans apart). `planId` is the plan being edited, whose own migrations do not count.
 */
/** A plan whose migration holds a VM (SDD §5.4), with that migration's phase. */
export interface Holder {
  planId: string;
  planName: string;
  phase: Phase;
}

/**
 * Each VM of the source provider `sourceId` that migrations of plans hold (SDD §5.4: not in a phase that releases it),
 * by source id, with the holding plans in the order of the migrations; `planId` is a plan whose own migrations do not
 * count (the plan being edited).
 */
export function holdersOf(
  sourceId: string,
  plans: readonly Plan[],
  migrations: readonly (Pick<Migration, 'plan_id' | 'phase'> & { vm: Pick<Migration['vm'], 'source_id'> })[],
  planId?: string,
): Map<string, Holder[]> {
  const others = new Map(plans.filter((p) => p.id !== planId && p.source_provider_id === sourceId).map((p) => [p.id, p]));
  const held = new Map<string, Holder[]>();
  for (const m of migrations) {
    const other = others.get(m.plan_id);
    if (!other || RELEASES_VM.has(m.phase)) continue;
    const holders = held.get(m.vm.source_id) ?? [];
    if (!holders.some((h) => h.planId === other.id)) holders.push({ planId: other.id, planName: other.name, phase: m.phase });
    held.set(m.vm.source_id, holders);
  }
  return held;
}

export function heldElsewhere(
  vmIds: Iterable<string>,
  sourceId: string,
  planId: string | undefined,
  plans: readonly Plan[],
  migrations: readonly (Pick<Migration, 'plan_id' | 'phase'> & { vm: Pick<Migration['vm'], 'source_id' | 'name'> })[],
): Map<string, string[]> {
  const wanted = new Set(vmIds);
  const others = new Map(plans.filter((p) => p.id !== planId && p.source_provider_id === sourceId).map((p) => [p.id, p]));
  const held = new Map<string, Set<string>>();
  for (const m of migrations) {
    const other = others.get(m.plan_id);
    if (!other || !wanted.has(m.vm.source_id) || RELEASES_VM.has(m.phase)) continue;
    const holders = held.get(m.vm.source_id) ?? new Set<string>();
    holders.add(`${m.vm.name} (plan "${other.name}", ${other.id}, ${m.phase})`);
    held.set(m.vm.source_id, holders);
  }
  return new Map([...held].sort(([a], [b]) => a.localeCompare(b)).map(([vmId, holders]) => [vmId, [...holders].sort()]));
}
