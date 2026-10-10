/**
 * The mock's port of the control plane's `derive_plan_status` (SDD §8.1, ADV-19); both suites test it
 * against one case table, `seamless/tests/fixtures/plan_status_cases.json`.
 */
import type { DerivedPlanStatus, Migration, Phase, Plan } from './types';

/** A migration in these is at work: the plan runs (or is paused). */
const AT_WORK: ReadonlySet<Phase> = new Set(['precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'rolling_back']);
/** Still to start in a running or paused plan. */
const TO_START: ReadonlySet<Phase> = new Set(['pending', 'ready', 'blocked', 'validating']);
const DONE: ReadonlySet<Phase> = new Set(['completed', 'finalized', 'rolled_back']);

export function derivePlanStatus(plan: Pick<Plan, 'id' | 'status'>, migrations: readonly Migration[]): DerivedPlanStatus {
  if (plan.status === 'draft') return 'draft';
  const phases = new Set(migrations.filter((m) => m.plan_id === plan.id && m.phase !== 'cancelled').map((m) => m.phase));
  if (phases.size === 0) return plan.status;
  if ([...phases].some((p) => AT_WORK.has(p))) return plan.status === 'paused' ? 'paused' : 'running';
  if ((plan.status === 'running' || plan.status === 'paused') && [...phases].some((p) => TO_START.has(p))) return plan.status;
  if (phases.has('failed')) return 'failed';
  if ([...phases].every((p) => DONE.has(p))) {
    if (phases.has('completed')) return 'completed';
    return phases.has('finalized') ? 'finalized' : 'completed';
  }
  return plan.status;
}
