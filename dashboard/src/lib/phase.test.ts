import { describe, expect, it } from 'vitest';
import { PHASES, STRATEGIES, type Phase } from '../api/types';
import type { Migration, SyncPass } from '../api/types';
import { happyPath, isActivePhase, isTerminalPhase, phaseMeta, preflightRan, runningStep, TONES } from './phase';

function isRenderableComponent(value: unknown): boolean {
  // lucide icons are forwardRef objects; plain function components are also fine.
  return (
    typeof value === 'function' ||
    (typeof value === 'object' && value !== null && '$$typeof' in (value as Record<string, unknown>))
  );
}

describe('phaseMeta', () => {
  it.each(PHASES)('%s has a label, a tone and an icon', (phase) => {
    const meta = phaseMeta(phase);
    expect(meta.label.trim().length).toBeGreaterThan(0);
    expect(TONES).toContain(meta.tone);
    expect(isRenderableComponent(meta.icon)).toBe(true);
    expect(meta.description.trim().length).toBeGreaterThan(0);
  });

  it('gives every phase a distinct label so status never depends on colour alone', () => {
    const labels = PHASES.map((phase) => phaseMeta(phase).label);
    expect(new Set(labels).size).toBe(PHASES.length);
  });

  it('marks failures as danger and success phases as success', () => {
    expect(phaseMeta('failed').tone).toBe('danger');
    expect(phaseMeta('blocked').tone).toBe('danger');
    expect(phaseMeta('completed').tone).toBe('success');
    expect(phaseMeta('finalized').tone).toBe('success');
    expect(phaseMeta('awaiting_cutover').tone).toBe('warning');
  });

  it('falls back gracefully for phases unknown to this build', () => {
    const meta = phaseMeta('hibernating' as Phase);
    expect(meta.label).toBe('hibernating');
    expect(meta.tone).toBe('neutral');
    expect(isRenderableComponent(meta.icon)).toBe(true);
  });
});

describe('phase helpers', () => {
  it('knows terminal and active phases (SDD §5.1, §8)', () => {
    expect(isTerminalPhase('finalized')).toBe(true);
    expect(isTerminalPhase('cancelled')).toBe(true);
    expect(isTerminalPhase('completed')).toBe(false);
    for (const phase of ['precopy', 'syncing', 'cutover', 'verifying', 'rolling_back'] as const) {
      expect(isActivePhase(phase)).toBe(true);
    }
    expect(isActivePhase('awaiting_cutover')).toBe(false);
  });

  it('builds the happy path per strategy family (SDD §5.2)', () => {
    expect(happyPath('warm')).toEqual([
      'pending',
      'validating',
      'ready',
      'precopy',
      'syncing',
      'awaiting_cutover',
      'cutover',
      'verifying',
      'completed',
      'finalized',
    ]);
    expect(happyPath('vmware_warm')).toEqual(happyPath('warm'));
    for (const strategy of ['cold', 'storage_handover', 'vmware_cold'] as const) {
      expect(happyPath(strategy)).toEqual([
        'pending',
        'validating',
        'ready',
        'cutover',
        'verifying',
        'completed',
        'finalized',
      ]);
    }
    expect(STRATEGIES).toHaveLength(5);
  });
});


describe('preflightRan', () => {
  const history = (...phases: Phase[]) => phases.map((to_phase, i) => ({ from_phase: i ? phases[i - 1]! : null, to_phase, at: '2026-10-08T12:00:00Z', reason: '', actor: 'test' }));

  it('is false until validation finished, and for a migration cancelled before it (SDD §16)', () => {
    expect(preflightRan({ phase: 'pending', phase_history: history('pending') })).toBe(false);
    expect(preflightRan({ phase: 'validating', phase_history: history('pending', 'validating') })).toBe(false);
    expect(preflightRan({ phase: 'cancelled', phase_history: history('pending', 'cancelled') })).toBe(false);
    for (const phase of ['blocked', 'ready', 'precopy', 'awaiting_cutover', 'completed', 'failed'] as Phase[]) {
      expect(preflightRan({ phase, phase_history: history('pending', 'validating', 'ready') })).toBe(true);
    }
    expect(preflightRan({ phase: 'cancelled', phase_history: history('pending', 'validating', 'blocked', 'cancelled') })).toBe(true);
  });
});

describe('runningStep', () => {
  const ended = (number: number, kind: SyncPass['kind']): SyncPass => ({
    number,
    kind,
    started_at: '2026-10-08T10:00:00Z',
    ended_at: '2026-10-08T10:10:00Z',
    bytes_scanned: 0,
    bytes_changed: 0,
    bytes_transferred: 0,
    duration_s: 600,
  });
  const step = (phase: Migration['phase'], strategy: Migration['strategy'], sync_passes: SyncPass[] = []) =>
    runningStep({ phase, strategy, sync_passes });

  it('names the running step from the phase and the passes that ended, as the API lists them (SDD §4.2, §16)', () => {
    expect(step('precopy', 'warm')).toEqual({ label: '#1 full', pass: { number: 1, kind: 'full' } });
    expect(step('syncing', 'warm', [ended(1, 'full'), ended(2, 'delta')])).toEqual({ label: '#3 delta', pass: { number: 3, kind: 'delta' } });
    expect(step('syncing', 'vmware_warm', [ended(1, 'full')])?.label).toBe('#2 delta');
    expect(step('cutover', 'warm', [ended(1, 'full'), ended(26, 'delta')])?.label).toBe('#27 final');
    expect(step('cutover', 'vmware_warm', [ended(1, 'full'), ended(2, 'delta')])?.label).toBe('#3 final');
    expect(step('cutover', 'cold')).toEqual({ label: '#1 full', pass: { number: 1, kind: 'full' } });
    expect(step('cutover', 'vmware_cold')?.label).toBe('#1 full');
    expect(step('cutover', 'storage_handover')).toEqual({ label: 'Volume handover', pass: null });
  });

  it('names no step outside the phases that run one', () => {
    for (const phase of ['pending', 'validating', 'ready', 'awaiting_cutover', 'verifying', 'completed', 'rolling_back'] as const) {
      expect(step(phase, 'warm', [ended(1, 'full')]), phase).toBeNull();
    }
  });

  it('counts a running pass the page lists (the mock) as the running step, not before it', () => {
    const running = { ...ended(2, 'delta'), ended_at: null, duration_s: null };
    expect(step('syncing', 'warm', [ended(1, 'full'), running])?.label).toBe('#2 delta');
  });
});
