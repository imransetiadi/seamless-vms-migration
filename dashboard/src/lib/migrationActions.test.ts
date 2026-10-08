import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { PHASES, type Migration, type Phase, type Strategy } from '../api/types';
import { migrationActions, nextStep, type MigrationActionKey } from './migrationActions';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const base = fx.migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;

function migration(phase: Phase, overrides: Partial<Migration> = {}, strategy: Strategy = 'warm'): Migration {
  return { ...base, phase, strategy, cutover_requested: false, downtime_started_at: null, downtime_ended_at: null, ...overrides };
}

const ENABLED_FOR_APPROVER: Record<Phase, MigrationActionKey[]> = {
  pending: ['cancel'],
  validating: [],
  blocked: ['cancel'],
  ready: ['approve', 'cancel'],
  precopy: ['approve', 'cancel'],
  syncing: ['approve', 'cancel'],
  awaiting_cutover: ['approve', 'cutover', 'sync', 'cancel'],
  cutover: ['rollback'],
  verifying: ['rollback'],
  completed: ['rollback', 'finalize'],
  finalized: [],
  failed: ['rollback', 'retry', 'cancel'],
  rolling_back: [],
  rolled_back: ['retry'],
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
    const warm = migrationActions(migration('ready', {}, 'warm'), 'approver').cutover;
    expect(warm.enabled).toBe(false);
    expect(warm.reason).toMatch(/converge/i);
  });

  it('does not request a cutover twice', () => {
    const cutover = migrationActions(migration('awaiting_cutover', { cutover_requested: true }), 'approver').cutover;
    expect(cutover.enabled).toBe(false);
    expect(cutover.reason).toMatch(/already requested/i);
  });

  it('refuses Cancel once the source VM was stopped (failed → cancelled needs no downtime)', () => {
    const stopped = migration('failed', { downtime_started_at: '2026-10-08T11:20:00Z' });
    const cancel = migrationActions(stopped, 'operator').cancel;
    expect(cancel.enabled).toBe(false);
    expect(cancel.reason).toMatch(/roll back instead/i);
    expect(migrationActions(stopped, 'operator').rollback.enabled).toBe(true);
  });

  it('gives viewers nothing and says which role is needed', () => {
    const actions = migrationActions(migration('awaiting_cutover'), 'viewer');
    expect(actions.sync).toEqual({ enabled: false, reason: 'Requires the operator role.' });
    expect(actions.cutover).toEqual({ enabled: false, reason: 'Requires the approver role.' });
  });

  it('names the next step', () => {
    expect(nextStep(migration('awaiting_cutover'), null).action).toBe('cutover');
    expect(nextStep(migration('completed'), null).action).toBe('finalize');
    expect(nextStep(migration('failed', { downtime_started_at: '2026-10-08T11:20:00Z' }), null).action).toBe('rollback');
    expect(nextStep(migration('precopy'), null).action).toBeNull();
    expect(nextStep(migration('precopy'), null).text).toMatch(/pre-copy/i);
  });
});
