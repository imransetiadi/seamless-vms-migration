import type { DecisionType, Migration, Plan, VMRef } from '../api/types';
import { DECISION_TYPE_LABELS, waitingDecision } from './advisor';
import { countedApprovals } from './migrationActions';
import { isWarmStrategy } from './phase';
import { PERMISSIONS } from './roles';
import { isWaveActive } from './waves';

/** What Pending approvals reads of a migration, or of its summary (SDD §4, §16). */
export type ApprovalMigration = Pick<
  Migration,
  'id' | 'plan_id' | 'wave_id' | 'strategy' | 'phase' | 'approvals' | 'cutover_requested' | 'cutover_requested_by' | 'force_cutover' | 'advisor_hold' | 'cutover_refusals'
> & { vm: Pick<VMRef, 'source_id' | 'name'> };

/** The two kinds of approval (SDD §16, ADV-20): the plan's cutover approval and an advisor decision (§14.6). */
export type ApprovalKind = 'cutover' | 'advisor_decision';

/** How many rows Pending approvals shows before "Show all", as many as Needs attention (SDD §16). */
export const APPROVALS_SHOWN = 8;

export const APPROVAL_KIND_LABELS: Record<ApprovalKind, string> = {
  cutover: 'Cutover approval',
  advisor_decision: 'Advisor decision',
};

export interface PendingApproval {
  kind: ApprovalKind;
  migration: ApprovalMigration;
  /** Undefined while the plans could not be loaded. */
  plan: Plan | undefined;
  /** An advisor decision's type; null for a cutover approval. */
  decisionType: DecisionType | null;
  /** What it waits for, in one sentence. */
  waitsFor: string;
  /** Where it is decided. */
  href: string;
}

/** "Cutover approval", or "Advisor decision · {decision type}". */
export function approvalKindLabel(item: Pick<PendingApproval, 'kind' | 'decisionType'>): string {
  const label = APPROVAL_KIND_LABELS[item.kind];
  return item.decisionType ? `${label} · ${DECISION_TYPE_LABELS[item.decisionType]}` : label;
}

/** At the cutover gate (SDD §5.4): converged, a single-shot strategy in ready whose wave runs, or a forced cutover. */
function atGate(m: ApprovalMigration, plan: Plan, migrations: readonly ApprovalMigration[]): boolean {
  if (m.phase === 'awaiting_cutover') return true;
  if (m.phase === 'ready') return !isWarmStrategy(m.strategy) && plan.status === 'running' && isWaveActive(plan, m.wave_id, migrations);
  return (m.phase === 'precopy' || m.phase === 'syncing') && m.force_cutover !== null;
}

function gateWait(m: ApprovalMigration, plan: Plan): string {
  // maker-checker: the requester's own approval does not count (SDD §5.4, ADV-17)
  if (plan.require_distinct_approver && m.force_cutover) return `Cutover forced by ${m.force_cutover.actor}: another approver approves it.`;
  if (plan.require_distinct_approver && m.cutover_requested && m.cutover_requested_by) return `Requested by ${m.cutover_requested_by}: another approver approves it.`;
  return m.phase === 'awaiting_cutover' ? 'Converged: waiting for an approval; keep-warm syncs run meanwhile.' : 'Ready for cutover: waiting for an approval.';
}

/** What a cutover approval waits for, and what its cutover will refuse: the approver sees it where the approval
 * is given (SDD §16). */
function cutoverWait(m: ApprovalMigration, plan: Plan): string {
  const n = m.cutover_refusals.length;
  if (n === 0) return gateWait(m, plan);
  return `${gateWait(m, plan)} The cutover will refuse ${n} problem${n === 1 ? '' : 's'}: fix ${n === 1 ? 'it' : 'them'} first.`;
}

/**
 * Every approval a person still has to give (SDD §16, ADV-20): a cutover approval while a migration waits at the
 * cutover gate of a plan that requires one and no approval counts — the window and the cutover slots are waits, not
 * approvals — and an advisor decision while it holds its migration (`advisor_hold`: a strategy decision, §14.6).
 * Cutover approvals first, then advisor decisions, each by VM name. Without the plans, the cutover approvals are
 * unknown (`cutoverUnknown`); the advisor decisions still show.
 */
export function pendingApprovals(
  migrations: readonly ApprovalMigration[],
  plans: ReadonlyMap<string, Plan>,
  { plansUnknown = false }: { plansUnknown?: boolean } = {},
): { items: PendingApproval[]; cutoverUnknown: boolean } {
  const cutover: PendingApproval[] = [];
  const advisor: PendingApproval[] = [];
  for (const m of migrations) {
    const plan = plans.get(m.plan_id);
    const held = waitingDecision(m);
    if (held) {
      advisor.push({
        kind: 'advisor_decision',
        migration: m,
        plan,
        decisionType: 'migration_strategy',
        waitsFor: `Approve or override the advisor's decision (${PERMISSIONS['advisor.approve']}); nothing starts before that.`,
        href: `/advisor/recommendations/${encodeURIComponent(held)}`,
      });
    }
    if (plan?.require_approval && atGate(m, plan, migrations) && countedApprovals(m, plan).length === 0) {
      cutover.push({ kind: 'cutover', migration: m, plan, decisionType: null, waitsFor: cutoverWait(m, plan), href: `/migrations/${encodeURIComponent(m.id)}` });
    }
  }
  const byName = (a: PendingApproval, b: PendingApproval) => a.migration.vm.name.localeCompare(b.migration.vm.name);
  return { items: [...cutover.sort(byName), ...advisor.sort(byName)], cutoverUnknown: plansUnknown };
}
