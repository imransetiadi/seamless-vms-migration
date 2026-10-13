import type { Migration, MigrationLike, MigrationSummary, Plan, PlanFinalizeResult, Wave } from '../api/types';

/** A migration, or its summary: what tells when it last entered Completed (SDD §4, §8.1). */
export type Completable = Pick<Migration, 'phase'> & ({ phase_history: Migration['phase_history'] } | Pick<MigrationSummary, 'phase_since'>);

/** When a migration last entered Completed (SDD §8.1): the start of its time in the finalize queue. */
export function completedSince(m: Completable): string | null {
  if (m.phase !== 'completed') return null;
  // a summary's phase began when it last entered Completed (SDD §4)
  if ('phase_since' in m) return m.phase_since;
  const entry = [...m.phase_history].reverse().find((c) => c.to_phase === 'completed');
  return entry?.at ?? null;
}

/** Waited in Completed longer than the plan's `finalize_warning_h` (SDD §8.1, ADV-19). */
export function isOverdue(m: Completable, warningH: number, now: number = Date.now()): boolean {
  const since = completedSince(m);
  return since !== null && now - Date.parse(since) > warningH * 3_600_000;
}

/** The migrations awaiting finalize, oldest first (SDD §8.1). */
export function finalizeQueue<M extends MigrationLike>(migrations: readonly M[]): M[] {
  return migrations
    .filter((m) => m.phase === 'completed')
    .sort((a, b) => Date.parse(completedSince(a) ?? '') - Date.parse(completedSince(b) ?? ''));
}

/** What a bulk finalize covers (SDD §8.1): the plan's `completed` migrations, oldest first, of the wave's
 * VMs (its `vm_ids`, as the API scopes it) when a wave is named. */
export function inScope<M extends MigrationLike>(plan: Plan, migrations: readonly M[], wave: Wave | null): M[] {
  return finalizeQueue(migrations).filter((m) => m.plan_id === plan.id && (!wave || wave.vm_ids.includes(m.vm.source_id)));
}

/**
 * What a bulk finalize of a scope does (SDD §8.1): it finalizes the VMs whose source is stopped and skips every VM
 * whose source VM runs again - one whose finalize began deleting it too, finalized again from its own page.
 */
export function bulkFinalize<M extends MigrationLike>(items: readonly M[]): { finalizes: M[]; skips: M[] } {
  return { finalizes: items.filter((m) => !m.source_running_at), skips: items.filter((m) => m.source_running_at) };
}

/** Why a bulk finalize is unavailable while the source VM of every VM in its scope runs again (SDD §8.1, §16). */
export function everySourceRunsText(skips: readonly MigrationLike[]): string {
  return skips.length === 1
    ? `The source VM of ${skips[0]?.vm.name} runs again: stop it, or roll it back, before finalizing.`
    : `The source VM of each of the ${skips.length} VMs awaiting finalize runs again: stop them, or roll them back, before finalizing.`;
}

/** The VMs a bulk finalize skips because their source VM runs again, named (SDD §16). */
export function skipsText(skips: readonly MigrationLike[]): string {
  return skips.length === 1
    ? `${skips[0]?.vm.name} is skipped: its source VM runs again. Stop it, or roll it back, before finalizing it.`
    : `${skips.length} VMs are skipped, as their source VMs run again: ${skips.map((m) => m.vm.name).join(', ')}. Stop them, or roll them back, before finalizing them.`;
}

export interface QueueGroup<M extends MigrationLike = MigrationLike> {
  /** null: the VMs in no wave. */
  wave: Wave | null;
  items: M[];
}

/** The finalize queue by wave in wave order, VMs in no wave last; a wave with nothing to finalize is left out. */
export function queueByWave<M extends MigrationLike>(plan: Plan, migrations: readonly M[]): QueueGroup<M>[] {
  const queue = inScope(plan, migrations, null);
  const waves = [...plan.waves].sort((a, b) => a.order - b.order);
  const groups = waves.map((wave) => ({ wave, items: queue.filter((m) => wave.vm_ids.includes(m.vm.source_id)) }));
  const loose = queue.filter((m) => !waves.some((w) => w.vm_ids.includes(m.vm.source_id)));
  return [...groups, { wave: null, items: loose }].filter((g) => g.items.length > 0);
}

/**
 * What a bulk finalize did, for the page's live region: the counts only — the API's reasons vary
 * (left Completed, a failed finalize), and the notice lists them (SDD §16).
 */
export function finalizedText(result: PlanFinalizeResult): string {
  const n = result.finalized.length;
  const done = `${n} VM${n === 1 ? '' : 's'} finalized.`;
  const skipped = result.skipped.length;
  return skipped ? `${done} ${skipped} not finalized: the page lists why.` : done;
}

export interface SkippedVm {
  id: string;
  name: string;
  reason: string;
}

/** The VMs a bulk finalize skipped, by name, each with the reason the API gave (SDD §8.1). */
export function skippedVms(result: PlanFinalizeResult, migrations: readonly MigrationLike[]): SkippedVm[] {
  const names = new Map(migrations.map((m) => [m.id, m.vm.name]));
  return result.skipped.map((s) => ({ id: s.migration_id, name: names.get(s.migration_id) ?? s.migration_id, reason: s.reason }));
}
