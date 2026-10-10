import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { PHASES, type Migration, type Phase, type Plan, type Strategy } from '../api/types';
import { clearedByStrategyChange, clearedByValidation, countedApprovals, migrationActions, nextStep, type MigrationActionKey } from './migrationActions';
import { OWN_CUTOVER } from './roles';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const base = fx.migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;
const FORCED = { actor: 'rina', at: '2026-10-08T11:00:00Z', reason: 'The change window closes in 20 minutes', delta_bytes: 2 ** 30, estimated_downtime_s: 320 };

function migration(phase: Phase, overrides: Partial<Migration> = {}, strategy: Strategy = 'warm'): Migration {
  return { ...base, phase, strategy, cutover_requested: false, downtime_started_at: null, downtime_ended_at: null, ...overrides };
}

const ENABLED_FOR_APPROVER: Record<Phase, MigrationActionKey[]> = {
  pending: ['cancel'],
  validating: [],
  blocked: ['cancel'],
  // a warm cutover may be requested before it converged; it starts once it does (SDD §5.4, §16)
  ready: ['approve', 'cutover', 'cancel'],
  // a Force cutover skips convergence while the passes run (SDD §5.4, ADV-16)
  precopy: ['approve', 'cutover', 'force-cutover', 'cancel'],
  syncing: ['approve', 'cutover', 'force-cutover', 'cancel'],
  awaiting_cutover: ['approve', 'cutover', 'sync', 'cancel'],
  cutover: ['rollback'],
  verifying: ['rollback'],
  completed: ['rollback', 'finalize'],
  finalized: [],
  // an approval given ahead of a retry lasts through it (SDD §5.1, §16)
  failed: ['approve', 'rollback', 'retry', 'cancel'],
  rolling_back: [],
  rolled_back: ['approve', 'retry'],
  cancelled: [],
};

function enabled(m: Migration, role: Parameters<typeof migrationActions>[1]): MigrationActionKey[] {
  const actions = migrationActions(m, role);
  return (Object.keys(actions) as MigrationActionKey[]).filter((k) => actions[k].enabled).sort();
}

describe('migrationActions (warm strategy, approver)', () => {
  it.each(PHASES)('%s', (phase) => {
    expect(enabled(migration(phase), 'approver')).toEqual([...ENABLED_FOR_APPROVER[phase]].sort());
  });
});

describe('migrationActions rules', () => {
  it('allows Finalize only for a completed migration and an approver', () => {
    expect(migrationActions(migration('completed'), 'approver').finalize.enabled).toBe(true);
    expect(migrationActions(migration('completed'), 'admin').finalize.enabled).toBe(true);
    const operator = migrationActions(migration('completed'), 'operator').finalize;
    expect(operator.enabled).toBe(false);
    expect(operator.reason).toMatch(/approver role/);
    const verifying = migrationActions(migration('verifying'), 'approver').finalize;
    expect(verifying.enabled).toBe(false);
    expect(verifying.reason).toMatch(/completed/i);
  });

  it('lets single-shot strategies cut over from ready (SDD §5.2)', () => {
    expect(migrationActions(migration('ready', {}, 'cold'), 'approver').cutover.enabled).toBe(true);
    // a warm migration may be requested from ready too: it cuts over once pre-copy converged
    expect(migrationActions(migration('ready', {}, 'warm'), 'approver').cutover.enabled).toBe(true);
  });

  it('offers Approve only where an approval lasts, and says why elsewhere (SDD §16)', () => {
    for (const phase of ['pending', 'validating', 'blocked'] as const) {
      const approve = migrationActions(migration(phase), 'approver').approve;
      expect(approve.enabled).toBe(false);
      expect(approve.reason).toMatch(/validation clears approvals/i);
    }
    for (const phase of ['cutover', 'verifying', 'completed'] as const) {
      expect(migrationActions(migration(phase), 'approver').approve.reason).toMatch(/cutover has started/i);
    }
  });

  it('says to validate the plan again instead of cancelling a migration being validated (SDD §5.1)', () => {
    const cancel = migrationActions(migration('validating'), 'operator').cancel;
    expect(cancel.enabled).toBe(false);
    expect(cancel.reason).toMatch(/being validated: validate the plan again/i);
  });

  it('does not request a cutover twice', () => {
    const cutover = migrationActions(migration('awaiting_cutover', { cutover_requested: true }), 'approver').cutover;
    expect(cutover.enabled).toBe(false);
    expect(cutover.reason).toMatch(/already requested/i);
  });

  it('lets an approver force a requested cutover that waits for a closed window (SDD §5.4)', () => {
    const now = Date.parse('2026-10-08T12:00:00Z');
    const plan: Plan = {
      ...(fx.plans.find((p) => p.id === base.plan_id) as Plan),
      cutover_window: { start: '2026-10-09T22:00:00Z', end: '2026-10-10T02:00:00Z' },
    };
    const requested = migration('awaiting_cutover', { cutover_requested: true, force_window: false });
    expect(migrationActions(requested, 'approver', plan, now).cutover.enabled).toBe(true);
    // nothing left to force: already forced, the window is open, or there is no window
    const refused = { enabled: false, reason: expect.stringMatching(/already requested/i) };
    expect(migrationActions({ ...requested, force_window: true }, 'approver', plan, now).cutover).toEqual(refused);
    const open: Plan = { ...plan, cutover_window: { start: '2026-10-08T11:00:00Z', end: '2026-10-08T13:00:00Z' } };
    expect(migrationActions(requested, 'approver', open, now).cutover).toEqual(refused);
    expect(migrationActions(requested, 'approver', { ...plan, cutover_window: null }, now).cutover).toEqual(refused);
    expect(migrationActions(requested, 'operator', plan, now).cutover).toEqual({ enabled: false, reason: expect.stringMatching(/approver role/i) });
    // the next step says so, and points at the action
    const step = nextStep(requested, plan, now);
    expect(step.text).toMatch(/waiting for the cutover window.*an approver can let it cut over outside the window/i);
    expect(step.action).toBe('cutover');
    expect(nextStep(requested, open, now).action).toBeNull();
  });

  it('treats the window as closed only outside [start, end], as the API does (SDD §5.4)', () => {
    const start = Date.parse('2026-10-09T22:00:00Z');
    const end = Date.parse('2026-10-10T02:00:00Z');
    const plan: Plan = {
      ...(fx.plans.find((p) => p.id === base.plan_id) as Plan),
      cutover_window: { start: '2026-10-09T22:00:00Z', end: '2026-10-10T02:00:00Z' },
    };
    const requested = migration('awaiting_cutover', { cutover_requested: true, force_window: false });
    const forcible = (now: number) => migrationActions(requested, 'approver', plan, now).cutover.enabled;
    // the API's CutoverWindow.contains is start <= now <= end: both ends belong to the window
    expect([forcible(start - 1), forcible(start), forcible(end), forcible(end + 1)]).toEqual([true, false, false, true]);
  });

  it('refuses Cancel once the source VM was stopped (failed → cancelled needs no downtime)', () => {
    const stopped = migration('failed', { downtime_started_at: '2026-10-08T11:20:00Z' });
    const cancel = migrationActions(stopped, 'operator').cancel;
    expect(cancel.enabled).toBe(false);
    expect(cancel.reason).toMatch(/roll back/i);
    expect(migrationActions(stopped, 'operator').rollback.enabled).toBe(true);
  });

  it('refuses Cancel in any phase while the source VM is stopped, and says what to do (SDD §5.1)', () => {
    const open = { downtime_started_at: '2026-10-08T11:20:00Z', downtime_ended_at: null };
    const failed = migrationActions(migration('failed', open), 'operator').cancel;
    expect(failed.enabled).toBe(false);
    expect(failed.reason).toMatch(/source VM is stopped.*roll back.*retry/i);
    // a retried cutover keeps the open clock (SDD §5.2): the way on is the cutover
    const ready = migrationActions(migration('ready', open), 'operator').cancel;
    expect(ready.enabled).toBe(false);
    expect(ready.reason).toMatch(/source VM is stopped.*cut it over/i);
    // a cutover in progress has its clock open by design: the reason stays the phase rule
    const cutover = migrationActions(migration('cutover', open), 'operator').cancel;
    expect(cutover.enabled).toBe(false);
    expect(cutover.reason).toMatch(/once cutover has started/i);
    // once the source runs again (closed clock), a cancel is possible
    const closed = migrationActions(migration('ready', { ...open, downtime_ended_at: '2026-10-08T11:40:00Z' }), 'operator').cancel;
    expect(closed.enabled).toBe(true);
  });

  it('gives viewers nothing and says which role is needed', () => {
    const actions = migrationActions(migration('awaiting_cutover'), 'viewer');
    expect(actions.sync).toEqual({ enabled: false, reason: 'Requires the operator role.' });
    expect(actions.cutover).toEqual({ enabled: false, reason: 'Requires the approver role.' });
  });

  it('says a retried cutover still has its source stopped (SDD §5.2)', () => {
    const open = { downtime_started_at: '2026-10-08T11:20:00Z', downtime_ended_at: null };
    const cold = nextStep(migration('ready', { ...open, strategy: 'cold' }), null);
    expect(cold.text).toMatch(/still stopped/i);
    expect(cold.text).not.toMatch(/stops for about/i);
    expect(cold.action).toBe('cutover');
    const warm = nextStep(migration('ready', open), null);
    expect(warm.text).toMatch(/still stopped/i);
    expect(warm.text).not.toMatch(/source VM keeps running/i);
  });

  it('names the advisor decision as the next step while it waits for approval: nothing starts meanwhile (SDD §5.4, §14.6)', () => {
    for (const strategy of ['cold', 'warm', 'storage_handover'] as const) {
      const step = nextStep(migration('ready', { strategy, advisor_hold: 'rec-000000000001' }), null);
      expect(step.text, strategy).toBe("Ready. Next: approve or override the advisor's decision (admin); nothing starts before that.");
      expect(step.action, strategy).toBeNull();
    }
  });

  it("keeps Approve from the cutover's requester when the plan requires a distinct approver (SDD §5.4, ADV-17)", () => {
    const plan = fx.plans.find((p) => p.id === base.plan_id)!;
    const requested = migration('awaiting_cutover', { cutover_requested: true, cutover_requested_by: 'sari', cutover_requested_at: '2026-10-08T11:00:00Z', approvals: [] });
    const now = Date.parse('2026-10-08T12:00:00Z');
    expect(migrationActions(requested, 'approver', { ...plan, require_distinct_approver: true }, now, 'sari').approve).toEqual({ enabled: false, reason: OWN_CUTOVER });
    expect(migrationActions(requested, 'approver', { ...plan, require_distinct_approver: true }, now, 'rina').approve.enabled).toBe(true);
    expect(migrationActions(requested, 'approver', { ...plan, require_distinct_approver: false }, now, 'sari').approve.enabled).toBe(true);
    // while the plan is loading, the API's default holds: a distinct approver
    expect(migrationActions(requested, 'approver', undefined, now, 'sari').approve.enabled).toBe(false);
  });

  it('counts the approvals the gate counts: all but the requester\'s under a distinct approver (SDD §5.4)', () => {
    const plan = fx.plans.find((p) => p.id === base.plan_id)!;
    const at = '2026-10-08T11:00:00Z';
    const m = migration('awaiting_cutover', { cutover_requested: true, cutover_requested_by: 'sari', cutover_requested_at: at, approvals: [{ actor: 'sari', at, comment: null }, { actor: 'rina', at, comment: 'Checked' }] });
    expect(countedApprovals(m, { ...plan, require_distinct_approver: true }).map((a) => a.actor)).toEqual(['rina']);
    expect(countedApprovals(m, { ...plan, require_distinct_approver: false }).map((a) => a.actor)).toEqual(['sari', 'rina']);
    // stored before ADV-17: no requester, every approval counts
    expect(countedApprovals({ ...m, cutover_requested_by: null }, { ...plan, require_distinct_approver: true })).toHaveLength(2);
  });

  it('says that a requested cutover waits for another approver, and that a request is not an approval (ADV-17)', () => {
    const plan = { ...fx.plans.find((p) => p.id === base.plan_id)!, require_approval: true, require_distinct_approver: true };
    const waiting = migration('awaiting_cutover', { cutover_requested: true, cutover_requested_by: 'sari', cutover_requested_at: '2026-10-08T11:00:00Z', approvals: [] });
    expect(nextStep(waiting, plan)).toEqual({ text: 'Cutover requested by sari. Next: another approver approves it; keep-warm syncs run meanwhile.', action: 'approve' });
    // a request stored before ADV-17 names nobody: no 'requested by null'
    expect(nextStep(migration('awaiting_cutover', { cutover_requested: true, cutover_requested_by: null, approvals: [] }), plan).text).not.toMatch(/null/);
    expect(nextStep(migration('awaiting_cutover', { approvals: [] }), plan).text).toBe(
      'Converged. Next: an approver requests the cutover and another approves it; keep-warm syncs run meanwhile.',
    );
    const cold = migration('ready', { approvals: [], estimate: { ...base.estimate!, downtime_s: 300 } }, 'cold');
    expect(nextStep(cold, plan).text).toBe('Ready. Next: an approver requests the cutover and another approves it; the source VM stops for about 5 min.');
    expect(nextStep(cold, { ...plan, require_distinct_approver: false }).text).toBe('Ready. Next: an approver starts the cutover; the source VM stops for about 5 min.');
  });

  it('offers Force cutover to an approver while the passes of a warm migration run, and says why elsewhere (SDD §5.4, ADV-16)', () => {
    expect(migrationActions(migration('syncing'), 'approver')['force-cutover']).toEqual({ enabled: true, reason: null });
    expect(migrationActions(migration('precopy'), 'approver')['force-cutover'].enabled).toBe(true);
    expect(migrationActions(migration('syncing'), 'operator')['force-cutover']).toEqual({ enabled: false, reason: 'Requires the approver role.' });
    for (const phase of ['ready', 'awaiting_cutover', 'cutover', 'completed'] as const) {
      expect(migrationActions(migration(phase), 'approver')['force-cutover'], phase).toEqual({
        enabled: false,
        reason: 'A Force cutover skips convergence: it applies while pre-copy or delta passes run.',
      });
    }
    expect(migrationActions(migration('ready', {}, 'cold'), 'approver')['force-cutover']).toEqual({ enabled: false, reason: 'Only a warm migration has passes to skip.' });
    const forced = migration('syncing', { force_cutover: FORCED });
    expect(migrationActions(forced, 'approver')['force-cutover']).toEqual({ enabled: false, reason: 'Already forced by rina: the cutover follows the running pass.' });
  });

  it('says a forced cutover follows the running pass, and who approves it (SDD §5.4, ADV-16, ADV-17)', () => {
    const plan = { ...fx.plans.find((p) => p.id === base.plan_id)!, require_approval: true, require_distinct_approver: true };
    const forced = migration('syncing', { force_cutover: FORCED, cutover_requested: true, cutover_requested_by: 'rina', cutover_requested_at: FORCED.at, approvals: [] });
    expect(nextStep(forced, plan)).toEqual({ text: 'Cutover forced by rina. Next: another approver approves it; it starts after the running pass.', action: 'approve' });
    const approved = { ...forced, approvals: [{ actor: 'sari', at: FORCED.at, comment: null }] };
    expect(nextStep(approved, plan)).toEqual({ text: 'Cutover forced by rina: it starts after the running pass, once the window and a free cutover slot allow.', action: null });
    expect(nextStep({ ...forced, phase: 'precopy' }, { ...plan, require_approval: false }).text).toBe(
      'Cutover forced by rina: it starts after the running pass, once the window and a free cutover slot allow.',
    );
  });

  it('says how long a completed migration has waited, and asks for finalize past the plan reminder (SDD §8.1, ADV-19)', () => {
    const now = Date.parse('2026-10-08T12:00:00Z');
    const completed = (hoursAgo: number) =>
      migration('completed', {
        phase_history: [
          { from_phase: 'cutover', to_phase: 'verifying', at: new Date(now - (hoursAgo + 1) * 3_600_000).toISOString(), reason: '', actor: 'orchestrator' },
          { from_phase: 'verifying', to_phase: 'completed', at: new Date(now - hoursAgo * 3_600_000).toISOString(), reason: '', actor: 'orchestrator' },
        ],
      });
    const plan = { ...(fx.plans[0] as Plan), finalize_warning_h: 72 };
    expect(nextStep(completed(10), plan, now)).toEqual({
      text: 'Verified on RHOSO 10 h ago. Next: finalize to clean up the source (approver), or roll back if needed.',
      action: 'finalize',
    });
    expect(nextStep(completed(80), plan, now)).toEqual({
      text: 'Verified on RHOSO 3 d ago. It has waited in Completed longer than the plan allows (72 h): finalize it or roll it back.',
      action: 'finalize',
    });
    // longer than, not as long as; the plan sets the reminder
    expect(nextStep(completed(72), plan, now).text).not.toMatch(/longer than/);
    expect(nextStep(completed(80), { ...plan, finalize_warning_h: 96 }, now).text).not.toMatch(/longer than/);
    expect(nextStep(completed(100), { ...plan, finalize_warning_h: 96 }, now).text).toMatch(/allows \(96 h\)/);
    // without its plan the reminder is unknown, so nothing is claimed
    expect(nextStep(completed(80), null, now).text).not.toMatch(/longer than/);
    // the source runs again: no finalize advice until it is stopped (SDD §8.1)
    expect(nextStep({ ...completed(80), source_running_at: '2026-10-08T11:00:00Z' }, plan, now)).toEqual({
      text: 'The source VM runs again, so its data diverges from RHOSO. Next: stop it in the source cloud, or roll it back; finalize once it is stopped.',
      action: null,
    });
    // no transition into Completed recorded: no time is claimed either
    expect(nextStep(migration('completed', { phase_history: [] }), plan, now).text).toBe(
      'Verified on RHOSO. Next: finalize to clean up the source (approver), or roll back if needed.',
    );
  });

  it('names the next step', () => {
    expect(nextStep(migration('awaiting_cutover'), null).action).toBe('cutover');
    expect(nextStep(migration('completed'), null).action).toBe('finalize');
    expect(nextStep(migration('failed', { downtime_started_at: '2026-10-08T11:20:00Z' }), null).action).toBe('rollback');
    expect(nextStep(migration('precopy'), null).action).toBeNull();
    expect(nextStep(migration('precopy'), null).text).toMatch(/pre-copy/i);
  });
});

describe('what re-validation or a strategy change clears (SDD §5.4)', () => {
  const approval = { actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null };

  it('counts approvals and cutover requests of migrations that have not started', () => {
    const ready = migration('ready', { approvals: [approval, approval], cutover_requested: true });
    const blocked = migration('blocked', { approvals: [approval] });
    // an in-flight migration keeps its approvals: a re-validation does not touch it
    const precopy = migration('precopy', { approvals: [approval], cutover_requested: true });
    expect(clearedByValidation([ready, blocked, precopy])).toBe('3 approvals and 1 cutover request');
    expect(clearedByValidation([precopy, migration('ready')])).toBeNull();
  });

  it('names what a strategy change clears for one migration', () => {
    expect(clearedByStrategyChange(migration('ready', { approvals: [approval], cutover_requested: true }))).toBe('1 approval and the cutover request');
    expect(clearedByStrategyChange(migration('ready', { cutover_requested: true }))).toBe('the cutover request');
    expect(clearedByStrategyChange(migration('ready'))).toBeNull();
  });
});
