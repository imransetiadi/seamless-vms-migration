import type { Migration, Phase, Plan, VMRef, Wave } from '../api/types';

/** Phases that complete a wave (SDD §5.1); `failed` blocks a wave until retried/rolled back/cancelled. */
export const WAVE_COMPLETE_PHASES: ReadonlySet<Phase> = new Set(['completed', 'finalized', 'cancelled', 'rolled_back']);

export type WaveState = 'complete' | 'active' | 'waiting';

/** What wave membership reads of a migration, or of its summary (SDD §4). */
export type WaveMember = Pick<Migration, 'plan_id' | 'wave_id' | 'phase'> & { vm: Pick<VMRef, 'source_id'> };

export function waveMigrations<M extends WaveMember>(wave: Wave, migrations: readonly M[], planId: string): M[] {
  return migrations.filter((m) => m.plan_id === planId && (m.wave_id === wave.id || (!m.wave_id && wave.vm_ids.includes(m.vm.source_id))));
}

/** Every migration of the wave it did not cancel rolled back: the wave ended and migrated nothing (SDD §16). */
export function waveRolledBack(members: ReadonlyArray<Pick<Migration, 'phase'>>): boolean {
  const kept = members.filter((m) => m.phase !== 'cancelled');
  return kept.length > 0 && kept.every((m) => m.phase === 'rolled_back');
}

export function isWaveComplete(plan: Plan, waveId: string, migrations: readonly WaveMember[]): boolean {
  const wave = plan.waves.find((w) => w.id === waveId);
  if (!wave) return true;
  return waveMigrations(wave, migrations, plan.id).every((m) => WAVE_COMPLETE_PHASES.has(m.phase));
}

/** A wave is active once every wave it depends on is complete (SDD §8). */
export function isWaveActive(plan: Plan, waveId: string | null, migrations: readonly WaveMember[]): boolean {
  if (!waveId) return true;
  const wave = plan.waves.find((w) => w.id === waveId);
  if (!wave) return true;
  return wave.depends_on.every((dep) => isWaveComplete(plan, dep, migrations));
}

export function waveState(plan: Plan, wave: Wave, migrations: readonly WaveMember[]): WaveState {
  if (isWaveComplete(plan, wave.id, migrations) && waveMigrations(wave, migrations, plan.id).length > 0) return 'complete';
  return isWaveActive(plan, wave.id, migrations) ? 'active' : 'waiting';
}
