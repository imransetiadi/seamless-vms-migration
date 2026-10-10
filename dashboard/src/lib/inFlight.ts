import type { Migration, Phase } from '../api/types';

/**
 * Phases in which a migration depends on the plan's providers, mappings and strategy as they were when it
 * started (SDD §12) — the API's IN_FLIGHT_PHASES; a completed one until it is finalized (§8.1).
 */
export const IN_FLIGHT_PHASES: ReadonlySet<Phase> = new Set<Phase>(['precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'rolling_back', 'completed']);

/** A migration in flight, or whose source VM is stopped, keeps its plan from being edited or re-waved (SDD §12). */
export function blocksPlanChange(m: Migration): boolean {
  return IN_FLIGHT_PHASES.has(m.phase) || (Boolean(m.downtime_started_at) && !m.downtime_ended_at);
}

/** What releases each kind of such migration, most urgent first (SDD §12), in the API's words. */
const RELEASES = [
  // a failed one only blocks while its cutover keeps the source stopped: the VM is down
  { action: 'roll back or retry', one: 'the failed migration whose source VM is stopped', many: 'the failed migrations whose source VM is stopped' },
  { action: 'finish, roll back or cancel', one: 'the migration in flight', many: 'the migrations in flight' },
  { action: 'finalize or roll back', one: 'the completed migration', many: 'the completed migrations' },
] as const;

export interface ReleaseGroup {
  /** What releases them, e.g. "finalize or roll back". */
  action: string;
  /** What they are, singular or plural as `names` asks, e.g. "the completed migrations". */
  what: string;
  /** Their VM names, sorted like the API sorts them. */
  names: string[];
}

/** The migrations that block a plan change, grouped by what releases them, most urgent first; no empty group. */
export function releaseGroups(busy: readonly Migration[]): ReleaseGroup[] {
  const names: string[][] = [[], [], []];
  for (const m of busy) names[m.phase === 'failed' ? 0 : m.phase === 'completed' ? 2 : 1]!.push(m.vm.name);
  return RELEASES.flatMap((r, i) => {
    const group = names[i]!.sort();
    return group.length ? [{ action: r.action, what: group.length === 1 ? r.one : r.many, names: group }] : [];
  });
}

/** The API's 409 of an edit or re-wave (`in_flight_refusal`, SDD §12): at most `limit` names per group. */
export function inFlightRefusal(busy: readonly Migration[], before: string, limit = 10): string {
  const clauses = releaseGroups(busy).map(({ action, what, names }) => {
    const rest = names.length - limit;
    return `${action} ${what} (${names.slice(0, limit).join(', ')}${rest > 0 ? ` and ${rest} more` : ''})`;
  });
  return `${clauses.join('; ')} before ${before}`;
}
