import type { Migration, Plan } from '../api/types';
import { formatRelative } from './format';
import type { Tone } from './phase';
import { isWaveActive } from './waves';

const SINGLE_SHOT = new Set(['cold', 'storage_handover', 'vmware_cold']);

export interface AttentionItem {
  migration: Migration;
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

/** What a human has to do next for a migration that is waiting at the cutover gate (SDD §5.4). */
function gateItem(m: Migration, plan: Plan | undefined, now: number, rank: number): AttentionItem | null {
  if (plan && plan.require_approval && m.approvals.length === 0) {
    return {
      migration: m,
      tone: 'warning',
      reason: m.phase === 'awaiting_cutover' ? 'Converged — waiting for approval' : 'Ready for cutover — waiting for approval',
      nextAction: 'Approve or request cutover (approver)',
      rank,
    };
  }
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

/** Migrations that need a human, most urgent first — one entry per migration. */
export function attentionItems(migrations: Migration[], plans: ReadonlyMap<string, Plan>, now = Date.now()): AttentionItem[] {
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
    } else if (m.phase === 'failed') {
      item = { migration: m, tone: 'danger', reason: m.error ? `Failed: ${m.error}` : 'Failed', nextAction: 'Retry, roll back or cancel (operator)', rank: 1 };
    } else if (m.review_required) {
      item = {
        migration: m,
        tone: 'warning',
        reason: `Review required${m.review_reason ? `: ${m.review_reason}` : ''}`,
        nextAction: 'Check the advisor notes and the destination VM',
        rank: 2,
      };
    } else if (m.phase === 'awaiting_cutover') {
      item = gateItem(m, plan, now, 3);
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
    }
    if (item) items.push(item);
  }
  return items.sort((a, b) => a.rank - b.rank || a.migration.vm.name.localeCompare(b.migration.vm.name));
}
