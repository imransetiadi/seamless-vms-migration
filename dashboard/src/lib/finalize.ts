import type { Migration, Plan, PlanFinalizeResult, Wave } from '../api/types';

/** When a migration last entered Completed (SDD §8.1): the start of its time in the finalize queue. */
export function completedSince(m: Migration): string | null {
  if (m.phase !== 'completed') return null;
  const entry = [...m.phase_history].reverse().find((c) => c.to_phase === 'completed');
  return entry?.at ?? null;
}

/** Waited in Completed longer than the plan's `finalize_warning_h` (SDD §8.1, ADV-19). */
export function isOverdue(m: Migration, warningH: number, now: number = Date.now()): boolean {
  const since = completedSince(m);
  return since !== null && now - Date.parse(since) > warningH * 3_600_000;
}

/** The migrations awaiting finalize, oldest first (SDD §8.1). */
export function finalizeQueue(migrations: readonly Migration[]): Migration[] {
  return migrations
    .filter((m) => m.phase === 'completed')
    .sort((a, b) => Date.parse(completedSince(a) ?? '') - Date.parse(completedSince(b) ?? ''));
}

/** What a bulk finalize covers (SDD §8.1): the plan's `completed` migrations, oldest first, of the wave's
 * VMs (its `vm_ids`, as the API scopes it) when a wave is named. */
export function inScope(plan: Plan, migrations: readonly Migration[], wave: Wave | null): Migration[] {
  return finalizeQueue(migrations).filter((m) => m.plan_id === plan.id && (!wave || wave.vm_ids.includes(m.vm.source_id)));
}

export interface QueueGroup {
  /** null: the VMs in no wave. */
  wave: Wave | null;
  items: Migration[];
}

/** The finalize queue by wave in wave order, VMs in no wave last; a wave with nothing to finalize is left out. */
export function queueByWave(plan: Plan, migrations: readonly Migration[]): QueueGroup[] {
  const queue = inScope(plan, migrations, null);
  const waves = [...plan.waves].sort((a, b) => a.order - b.order);
  const groups = waves.map((wave) => ({ wave, items: queue.filter((m) => wave.vm_ids.includes(m.vm.source_id)) }));
  const loose = queue.filter((m) => !waves.some((w) => w.vm_ids.includes(m.vm.source_id)));
  return [...groups, { wave: null, items: loose }].filter((g) => g.items.length > 0);
}

/** What a bulk finalize did, in words: "2 VMs finalized. 1 skipped: it left Completed meanwhile." */
export function finalizedText(result: PlanFinalizeResult): string {
  const n = result.finalized.length;
  const done = `${n} VM${n === 1 ? '' : 's'} finalized.`;
  const skipped = result.skipped.length;
  return skipped ? `${done} ${skipped} skipped: ${skipped === 1 ? 'it' : 'they'} left Completed meanwhile.` : done;
}
