import type { Migration, MigrationSummary } from '../api/types';

export { MIGRATION_SUMMARY_FIELDS } from '../api/types';

/**
 * A migration's summary, as the API builds it for `GET /migrations?view=summary` (SDD §4, §12): its list fields as it
 * has them, the VM by id and name, and when its phase began - the last phase change's time, else its creation time.
 */
export function summaryOf(m: Migration): MigrationSummary {
  return {
    id: m.id,
    plan_id: m.plan_id,
    wave_id: m.wave_id,
    vm: { source_id: m.vm.source_id, name: m.vm.name },
    strategy: m.strategy,
    phase: m.phase,
    phase_since: m.phase_history.at(-1)?.at ?? m.created_at,
    progress_pct: m.progress_pct,
    downtime_started_at: m.downtime_started_at,
    downtime_ended_at: m.downtime_ended_at,
    approvals: m.approvals,
    cutover_requested: m.cutover_requested,
    cutover_requested_by: m.cutover_requested_by,
    force_cutover: m.force_cutover,
    force_window: m.force_window,
    advisor_hold: m.advisor_hold,
    review_required: m.review_required,
    review_reason: m.review_reason,
    error: m.error,
    source_running_at: m.source_running_at,
    findings: m.findings,
    created_at: m.created_at,
    updated_at: m.updated_at,
  };
}

/** Whether a cached `['migrations', query]` list holds summaries (SDD §16). */
export function isSummaryList(queryKey: readonly unknown[]): boolean {
  const query = queryKey[1];
  return typeof query === 'object' && query !== null && (query as { view?: string }).view === 'summary';
}
