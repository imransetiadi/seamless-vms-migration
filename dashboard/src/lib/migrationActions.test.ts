import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { PHASES, type Migration, type Phase, type Plan, type Strategy } from '../api/types';
import { formatDateTime, formatDuration } from './format';
import { approveDialog, clearedByStrategyChange, clearedByValidation, countedApprovals, migrationActions, nextStep, requestWaitsForApprover, uncountedReason, type MigrationActionKey } from './migrationActions';
import { AUTH_OFF_HINT, OWN_CUTOVER, OWN_FORCE } from './roles';

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

  it('keeps Finalize unavailable while the source VM runs again, as the API refuses it, unless a finalize began deleting it (SDD §8.1)', () => {
    const running = migrationActions(migration('completed', { source_running_at: '2026-10-08T11:00:00Z' }), 'approver');
    expect(running.finalize).toEqual({ enabled: false, reason: 'The source VM runs again: stop it, or roll it back, before finalizing.' });
    expect(running.rollback.enabled).toBe(true);
    // its finalize began deleting the source and a rollback is refused then: finalizing again is the way out
    const finalizing = migrationActions(migration('completed', { source_running_at: '2026-10-08T11:00:00Z', checkpoint: 'finalizing' }), 'approver');
    expect([finalizing.finalize.enabled, finalizing.rollback.enabled]).toEqual([true, false]);
  });

  it('keeps Rollback from a completed migration whose finalize began deleting the source, and offers Finalize again (SDD §8.1)', () => {
    const interrupted = migrationActions(migration('completed', { checkpoint: 'finalizing' }), 'approver');
    expect(interrupted.rollback.enabled).toBe(false);
    expect(interrupted.rollback.reason).toBe('The finalize began deleting the source VM: finalize it again.');
    expect(interrupted.finalize.enabled).toBe(true);
    // a completed migration whose finalize never began keeps its rollback
    expect(migrationActions(migration('completed', { checkpoint: 'verified' }), 'approver').rollback.enabled).toBe(true);
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

  it('says, with authentication off, how to get past a maker-checker refusal (SDD §13, §16)', () => {
    const plan = { ...fx.plans.find((p) => p.id === base.plan_id)!, require_distinct_approver: true };
    const now = Date.parse('2026-10-08T12:00:00Z');
    // every request is anonymous with authentication off: nobody else exists to approve
    const requested = migration('awaiting_cutover', { cutover_requested: true, cutover_requested_by: 'anonymous', approvals: [] });
    expect(migrationActions(requested, 'admin', plan, now, 'anonymous').approve).toEqual({ enabled: false, reason: `${OWN_CUTOVER} ${AUTH_OFF_HINT}` });
    const forced = migration('syncing', { force_cutover: { ...FORCED, actor: 'anonymous' }, approvals: [] });
    expect(migrationActions(forced, 'admin', plan, now, 'anonymous').approve).toEqual({ enabled: false, reason: `${OWN_FORCE} ${AUTH_OFF_HINT}` });
    // a named user gets the API's words alone
    expect(migrationActions({ ...requested, cutover_requested_by: 'sari' }, 'approver', plan, now, 'sari').approve.reason).toBe(OWN_CUTOVER);
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

  it('holds the approver who forced the cutover to the requester\'s rule, also when another user requested it (SDD §5.4)', () => {
    const plan = fx.plans.find((p) => p.id === base.plan_id)!;
    const at = '2026-10-08T11:00:00Z';
    const forced = migration('awaiting_cutover', {
      cutover_requested: true,
      cutover_requested_by: 'bob',
      cutover_requested_at: at,
      force_cutover: { actor: 'sari', at, reason: 'the window closes', delta_bytes: 0, estimated_downtime_s: 60 },
      approvals: [{ actor: 'sari', at, comment: null }, { actor: 'rina', at, comment: 'Checked' }],
    });
    const now = Date.parse('2026-10-08T12:00:00Z');
    const distinct = { ...plan, require_distinct_approver: true };
    expect(migrationActions(forced, 'approver', distinct, now, 'sari').approve).toEqual({ enabled: false, reason: OWN_FORCE });
    expect(migrationActions(forced, 'approver', distinct, now, 'rina').approve.enabled).toBe(true);
    expect(migrationActions(forced, 'approver', { ...plan, require_distinct_approver: false }, now, 'sari').approve.enabled).toBe(true);
    expect(countedApprovals(forced, distinct).map((a) => a.actor)).toEqual(['rina']);
    expect(countedApprovals(forced, { ...plan, require_distinct_approver: false }).map((a) => a.actor)).toEqual(['sari', 'rina']);
    // why an approval does not count (SDD §5.4, §16): the forcer forced it, the requester requested it, and a forcer
    // who also requested it (the force is the request when none was made) forced it
    expect([uncountedReason(forced, 'sari'), uncountedReason(forced, 'bob')]).toEqual(['forced the cutover', 'requested the cutover']);
    expect(uncountedReason({ ...forced, cutover_requested_by: 'sari' }, 'sari')).toBe('forced the cutover');
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

  it('knows when a cutover request only waits for another approver (maker-checker, SDD §5.4, §16)', () => {
    const plan = { ...fx.plans.find((p) => p.id === base.plan_id)!, require_approval: true, require_distinct_approver: true };
    const at = '2026-10-08T11:00:00Z';
    const ready = migration('ready', { approvals: [] }, 'cold');
    expect(requestWaitsForApprover(ready, plan, 'sari')).toBe(true);
    // another approver approved ahead: sari's request starts it
    expect(requestWaitsForApprover({ ...ready, approvals: [{ actor: 'rina', at, comment: null }] }, plan, 'sari')).toBe(false);
    // sari's own approval stops counting once she requests
    expect(requestWaitsForApprover({ ...ready, approvals: [{ actor: 'sari', at, comment: null }] }, plan, 'sari')).toBe(true);
    // the request already made by rina: sari's approval counts for it
    const requested = { ...ready, cutover_requested: true, cutover_requested_by: 'rina', approvals: [{ actor: 'sari', at, comment: null }] };
    expect(requestWaitsForApprover(requested, plan, 'sari')).toBe(false);
    // without maker-checker the request is the approval, and without approval nothing waits
    expect(requestWaitsForApprover(ready, { ...plan, require_distinct_approver: false }, 'sari')).toBe(false);
    expect(requestWaitsForApprover(ready, { ...plan, require_approval: false }, 'sari')).toBe(false);
  });

  const REFUSAL = "the NIC 00:50:56:40:d5:b3 of dc2-portal-02 on 'dc2-prod': port demo-port-1 holds 10.2.0.32";

  it('says to fix what the cutover will refuse, then sync, while the migration waits for its cutover (SDD §16)', () => {
    const plan = { ...fx.plans.find((p) => p.id === base.plan_id)!, require_approval: false };
    const fixFirst = {
      text: 'Converged, but the cutover will refuse what the last pass found. Next: fix it, then sync so a pass looks again (operator); keep-warm syncs look too.',
      action: 'sync',
    };
    expect(nextStep(migration('awaiting_cutover', { cutover_refusals: [REFUSAL] }), plan)).toEqual(fixFirst);
    // requested or not: the requested cutover would refuse it as well (requesting stays possible)
    const requested = migration('awaiting_cutover', { cutover_refusals: [REFUSAL], cutover_requested: true, cutover_requested_by: 'sari', approvals: [] });
    expect(nextStep(requested, { ...plan, require_approval: true, require_distinct_approver: true })).toEqual(fixFirst);
    // once a pass finds none, the cutover is next again
    expect(nextStep(migration('awaiting_cutover', { cutover_refusals: [] }), plan).action).toBe('cutover');
    // while the passes run, the next pass looks again by itself
    expect(nextStep(migration('syncing', { cutover_refusals: [REFUSAL] }), plan).text).toBe('Delta passes run until the change rate converges.');
  });

  it("says a cutover failed on what it refused, and that the retry's pass looks again (SDD §16)", () => {
    const refused = migration('failed', { cutover_refusals: [REFUSAL], error: `cutover: ${REFUSAL}` });
    expect(nextStep(refused, null)).toEqual({
      text: "Failed: the cutover refused what the passes found, before the source stopped. Next: fix it, then retry — the retry's pass looks again — or cancel.",
      action: 'retry',
    });
    // another step's failure keeps its own next step, whatever the passes found, and so does a cutover that
    // failed on something the passes did not find
    expect(nextStep(migration('failed', { cutover_refusals: [REFUSAL], error: 'sync: the NBD export closed' }), null).text).toBe(
      'Failed. Next: fix the cause and retry, or cancel.',
    );
    expect(nextStep(migration('failed', { cutover_refusals: [], error: 'cutover: a server named app-billing-01 exists' }), null).text).toBe(
      'Failed. Next: fix the cause and retry, or cancel.',
    );
    // after the source stopped, rolling back comes first, as before
    expect(nextStep(migration('failed', { cutover_refusals: [REFUSAL], error: `cutover: ${REFUSAL}`, downtime_started_at: '2026-10-08T11:00:00Z' }), null).action).toBe('rollback');
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
    // its finalize began deleting the source: finalizing again is the way out, never a rollback - also while the source
    // runs again (SDD §8.1)
    const again = { text: 'The finalize began deleting the source VM. Next: finalize it again (approver); it can no longer be rolled back.', action: 'finalize' };
    for (const m of [{ ...completed(10), checkpoint: 'finalizing' }, { ...completed(80), checkpoint: 'finalizing', source_running_at: '2026-10-08T11:00:00Z' }]) {
      expect(nextStep(m, plan, now)).toEqual(again);
    }
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

describe('approveDialog (SDD §5.4, §16)', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');
  const gate: Plan = {
    ...(fx.plans.find((p) => p.id === base.plan_id) as Plan),
    require_approval: true,
    require_distinct_approver: true,
    auto_cutover: false,
    cutover_window: null,
  };
  const later = { start: '2026-10-09T22:00:00Z', end: '2026-10-10T02:00:00Z' };
  // another approver requested it and none counts yet: the approval is the one the gate waits for
  const requested = (phase: Phase, overrides: Partial<Migration> = {}, strategy: Strategy = 'cold') =>
    migration(phase, { approvals: [], cutover_requested: true, cutover_requested_by: 'rina', advisor_hold: null, force_cutover: null, force_window: false, ...overrides }, strategy);
  const estimate = (m: Migration) => `Estimated downtime ${formatDuration(m.estimate!.downtime_s)} against an SLO of ${formatDuration(gate.downtime_slo_s)}.`;

  it('says the approval starts the cutover, and stops the source VM, when it is all the gate waits for', () => {
    for (const m of [requested('ready'), requested('awaiting_cutover', {}, 'warm')]) {
      expect(approveDialog(m, gate, now)).toEqual({
        description: `Your approval lets the cutover start: the source VM will then be stopped and the downtime clock starts as soon as the plan, its wave and a free cutover slot allow. ${estimate(m)}`,
        confirmLabel: 'Approve and cut over',
      });
    }
  });

  it('needs no request in a plan that cuts over automatically, nor a window it bypasses', () => {
    expect(approveDialog(requested('ready', { cutover_requested: false, cutover_requested_by: null }), { ...gate, auto_cutover: true }, now).confirmLabel).toBe('Approve and cut over');
    expect(approveDialog(requested('ready', { force_window: true }), { ...gate, cutover_window: later }, now).confirmLabel).toBe('Approve and cut over');
    expect(approveDialog(requested('ready'), { ...gate, cutover_window: { start: '2026-10-08T11:00:00Z', end: '2026-10-08T13:00:00Z' } }, now).confirmLabel).toBe('Approve and cut over');
  });

  it('starts a warm cutover once pre-copy converges, or after the running pass when forced', () => {
    const early = requested('precopy', {}, 'warm');
    expect(approveDialog(early, gate, now).description).toBe(
      `Your approval lets the cutover start: the source VM will then be stopped and the downtime clock starts once pre-copy has converged and the plan, its wave and a free cutover slot allow. ${estimate(early)}`,
    );
    const forced = requested('syncing', { cutover_requested_by: 'rina', force_cutover: FORCED }, 'warm');
    expect(approveDialog(forced, gate, now)).toEqual({
      description: `Your approval lets the cutover start: the source VM will then be stopped and the downtime clock starts after the running pass, once the plan, its wave and a free cutover slot allow. Estimated downtime ${formatDuration(FORCED.estimated_downtime_s)} against an SLO of ${formatDuration(gate.downtime_slo_s)}.`,
      confirmLabel: 'Approve and cut over',
    });
  });

  it('names what the cutover still waits for, with Approve', () => {
    const window = `the cutover window (${formatDateTime(later.start)} → ${formatDateTime(later.end)})`;
    expect(approveDialog(requested('ready', { cutover_requested: false, cutover_requested_by: null }), gate, now)).toEqual({
      description: 'Records your approval. The cutover still waits for a cutover request.',
      confirmLabel: 'Approve',
    });
    expect(approveDialog(requested('ready'), { ...gate, cutover_window: later }, now).description).toBe(`Records your approval. The cutover still waits for ${window}.`);
    // a window that has ended holds the cutover too
    const ended = { start: '2026-10-07T22:00:00Z', end: '2026-10-08T02:00:00Z' };
    expect(approveDialog(requested('ready'), { ...gate, cutover_window: ended }, now).description).toBe(
      `Records your approval. The cutover still waits for the cutover window (${formatDateTime(ended.start)} → ${formatDateTime(ended.end)}).`,
    );
    expect(approveDialog(requested('ready', { cutover_requested: false, cutover_requested_by: null }), { ...gate, cutover_window: later }, now).description).toBe(
      `Records your approval. The cutover still waits for a cutover request and ${window}.`,
    );
    expect(approveDialog(requested('ready', { advisor_hold: 'rec-1' }), gate, now)).toEqual({
      description: "Records your approval. The cutover still waits for the advisor's decision.",
      confirmLabel: 'Approve',
    });
  });

  it('says when an approval counts already, the plan needs none, a retry keeps it or the plan is not loaded', () => {
    const counted = requested('ready', { approvals: [{ actor: 'bayu', at: '2026-10-08T11:30:00Z', comment: null }] });
    expect(approveDialog(counted, gate, now)).toEqual({ description: 'Records your approval. The cutover has the approval it needs already.', confirmLabel: 'Approve' });
    expect(approveDialog(requested('ready'), { ...gate, require_approval: false }, now)).toEqual({
      description: 'This plan needs no approval to cut over: yours is recorded in the audit trail.',
      confirmLabel: 'Approve',
    });
    for (const phase of ['failed', 'rolled_back'] as const) {
      expect(approveDialog(requested(phase), gate, now)).toEqual({
        description: 'Records your approval for the next attempt: a retry keeps it, and its cutover is requested anew.',
        confirmLabel: 'Approve',
      });
    }
    for (const plan of [null, undefined]) {
      expect(approveDialog(requested('ready'), plan, now)).toEqual({ description: 'Records your approval. What it leads to depends on the plan, which is not loaded.', confirmLabel: 'Approve' });
    }
  });
});
