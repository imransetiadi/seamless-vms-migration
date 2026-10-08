import { describe, expect, it } from 'vitest';
import { PHASES, STRATEGIES, type Phase } from '../api/types';
import { happyPath, isActivePhase, isTerminalPhase, phaseMeta, TONES } from './phase';

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
