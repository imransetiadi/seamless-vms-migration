import type { Migration, Plan } from '../api/types';
import type { ApprovalMigration } from './approvals';
import { isOverdue, type Completable } from './finalize';
import { countedApprovals } from './migrationActions';
import { formatRelative } from './format';
import type { Tone } from './phase';
import { isWaveActive } from './waves';

const SINGLE_SHOT = new Set(['cold', 'storage_handover', 'vmware_cold']);
/** Where what the warm passes found still stands before the cutover (SDD §7.2, §16). */
const REFUSABLE = new Set(['precopy', 'syncing', 'awaiting_cutover']);

/** What Needs attention reads of a migration, or of its summary (SDD §4, §16). */
export type AttentionMigration = ApprovalMigration &
  Completable &
  Pick<
    Migration,
    'downtime_started_at' | 'downtime_ended_at' | 'source_running_at' | 'error' | 'review_required' | 'review_reason' | 'findings' | 'cutover_refusals'
  >;

export interface AttentionItem {
  migration: AttentionMigration;
  tone: Extract<Tone, 'danger' | 'warning' | 'info'>;
  reason: string;
  nextAction: string;
  /** Lower is more urgent. */
  rank: number;
}

function windowState(plan: Plan | undefined, now: number): 'none' | 'open' | 'closed' {
  const window = plan?.cutover_window;
  if (!window) return 'none';
  const start = Date.parse(window.start);
  const end = Date.parse(window.end);
  return now >= start && now <= end ? 'open' : 'closed';
}

/**
 * What a human has to do next for a migration that is waiting at the cutover gate (SDD §5.4). An approval still
 * to give is listed once on the Overview, in Pending approvals (lib/approvals.ts, SDD §16, ADV-20): the gate's
 * own count, so the requester's approval does not count under maker-checker.
 */
function gateItem(m: AttentionMigration, plan: Plan | undefined, now: number, rank: number): AttentionItem | null {
  if (plan && plan.require_approval && countedApprovals(m, plan).length === 0) return null;
  if (plan && !plan.auto_cutover && !m.cutover_requested) {
    return {
      migration: m,
      tone: 'warning',
      reason: 'Approved — waiting for a cutover request',
      nextAction: 'Request cutover (approver)',
      rank,
    };
  }
  if (windowState(plan, now) === 'closed' && plan?.cutover_window) {
    return {
      migration: m,
      tone: 'info',
      reason: `Waiting for the cutover window (opens ${formatRelative(plan.cutover_window.start, now)})`,
      nextAction: 'Cut over with "force window" if the change allows it (approver)',
      rank: rank + 3,
    };
  }
  return null;
}

/**
 * Migrations that need a human, most urgent first — one entry per migration. When the plans could not be
 * loaded (`plansUnknown`), a converged migration still waits at the cutover gate: listed with what it waits
 * for unknown, never left out (SDD §16).
 */
export function attentionItems(
  migrations: readonly AttentionMigration[],
  plans: ReadonlyMap<string, Plan>,
  now = Date.now(),
  { plansUnknown = false }: { plansUnknown?: boolean } = {},
): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const m of migrations) {
    const plan = plans.get(m.plan_id);
    let item: AttentionItem | null = null;
    if (m.phase === 'failed' && m.downtime_started_at && !m.downtime_ended_at) {
      item = {
        migration: m,
        tone: 'danger',
        reason: 'Failed after cutover started — the source VM is stopped',
        nextAction: 'Roll back to restart the source, or retry (operator)',
        rank: 0,
      };
    } else if (m.phase === 'failed' && m.downtime_started_at && m.error?.startsWith('rollback: ')) {
      // nothing the migration records tells whether its failed rollback started the source: it may still be stopped
      // (SDD §5.2, §16, review RV34B-03)
      item = {
        migration: m,
        tone: 'danger',
        reason: `Failed: ${m.error}`,
        nextAction: 'Check whether the source VM runs, then roll back again (operator)',
        rank: 0,
      };
    } else if (m.phase === 'completed' && m.source_running_at) {
      // the source watch saw it running again (SDD §8.1): what it writes no longer reaches RHOSO
      item = {
        migration: m,
        tone: 'danger',
        reason: 'The source VM runs again: its data diverges from the destination',
        nextAction: 'Stop the source VM, or roll it back (operator)',
        rank: 1,
      };
    } else if (m.phase === 'failed') {
      // only what the migration can take: a cancel is refused once the source was stopped (SDD §5.1, §16, reviews
      // RV32A-05, RV34B-03) - a failed rollback here never stopped its source
      const nextAction = m.error?.startsWith('rollback: ')
        ? 'Roll back again or cancel (operator)'
        : m.downtime_started_at
          ? 'Retry or roll back (operator)'
          : 'Retry, roll back or cancel (operator)';
      item = { migration: m, tone: 'danger', reason: m.error ? `Failed: ${m.error}` : 'Failed', nextAction, rank: 1 };
    } else if (m.review_required) {
      item = {
        migration: m,
        tone: 'warning',
        reason: `Review required${m.review_reason ? `: ${m.review_reason}` : ''}`,
        nextAction: 'Check the advisor notes and the destination VM',
        rank: 2,
      };
    } else if (m.cutover_refusals.length > 0 && REFUSABLE.has(m.phase)) {
      // SDD §16: fixing what the cutover will refuse comes before what it waits for; an approval stays in
      // Pending approvals
      const n = m.cutover_refusals.length;
      const fix = n === 1 ? 'Fix it' : 'Fix them';
      item = {
        migration: m,
        tone: 'warning',
        reason: `Cutover will refuse ${n} problem${n === 1 ? '' : 's'}`,
        nextAction:
          m.phase === 'awaiting_cutover' ? `${fix}, then Sync now (operator)` : `${fix} before it converges: the next pass looks again (operator)`,
        rank: 3,
      };
    } else if (m.phase === 'awaiting_cutover') {
      item =
        plan || !plansUnknown
          ? gateItem(m, plan, now, 3)
          : {
              migration: m,
              tone: 'warning',
              reason: 'Converged — waiting at the cutover gate',
              nextAction: 'Load the plans again to see whether it waits for an approval, a cutover request or its window',
              rank: 3,
            };
    } else if (
      m.phase === 'ready' &&
      SINGLE_SHOT.has(m.strategy) &&
      plan?.status === 'running' &&
      isWaveActive(plan, m.wave_id, migrations)
    ) {
      item = gateItem(m, plan, now, 3);
    } else if (m.phase === 'blocked') {
      const blockers = m.findings.filter((f) => f.severity === 'blocker');
      item = {
        migration: m,
        tone: 'danger',
        reason: blockers.length
          ? `Blocked by ${blockers.map((f) => f.code).join(', ')}`
          : 'Blocked by pre-flight findings',
        nextAction: 'Fix the finding, then re-validate the plan (operator)',
        rank: 4,
      };
    } else if (m.phase === 'rolling_back') {
      item = { migration: m, tone: 'info', reason: 'Rolling back — restarting the source VM', nextAction: 'Watch the rollback; retry when fixed', rank: 5 };
    } else if (m.phase === 'completed' && plan && isOverdue(m, plan.finalize_warning_h, now)) {
      // past the plan's finalize reminder (SDD §8.1, ADV-19); without its plan the reminder is unknown
      item = {
        migration: m,
        tone: 'warning',
        reason: `Waiting for finalize longer than the plan allows (${plan.finalize_warning_h} h)`,
        nextAction: 'Finalize or roll it back (approver)',
        rank: 6,
      };
    } else if (m.phase === 'rolled_back') {
      // SDD §16: it waits for a person to fix the cause and retry it, or to cancel it - the least urgent entry
      item = { migration: m, tone: 'info', reason: 'Rolled back — the source VM runs again', nextAction: 'Fix the cause, then retry or cancel (operator)', rank: 7 };
    }
    if (item) items.push(item);
  }
  return items.sort((a, b) => a.rank - b.rank || a.migration.vm.name.localeCompare(b.migration.vm.name));
}
