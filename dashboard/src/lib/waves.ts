import type { Migration, Phase, Plan, Wave } from '../api/types';

/** Phases that complete a wave (SDD §5.1); `failed` blocks a wave until retried/rolled back/cancelled. */
export const WAVE_COMPLETE_PHASES: ReadonlySet<Phase> = new Set(['completed', 'finalized', 'cancelled', 'rolled_back']);

export type WaveState = 'complete' | 'active' | 'waiting';

export function waveMigrations(wave: Wave, migrations: Migration[], planId: string): Migration[] {
  return migrations.filter((m) => m.plan_id === planId && (m.wave_id === wave.id || (!m.wave_id && wave.vm_ids.includes(m.vm.source_id))));
}

export function isWaveComplete(plan: Plan, waveId: string, migrations: Migration[]): boolean {
  const wave = plan.waves.find((w) => w.id === waveId);
  if (!wave) return true;
  return waveMigrations(wave, migrations, plan.id).every((m) => WAVE_COMPLETE_PHASES.has(m.phase));
}

/** A wave is active once every wave it depends on is complete (SDD §8). */
export function isWaveActive(plan: Plan, waveId: string | null, migrations: Migration[]): boolean {
  if (!waveId) return true;
  const wave = plan.waves.find((w) => w.id === waveId);
  if (!wave) return true;
  return wave.depends_on.every((dep) => isWaveComplete(plan, dep, migrations));
}

export function waveState(plan: Plan, wave: Wave, migrations: Migration[]): WaveState {
  if (isWaveComplete(plan, wave.id, migrations) && waveMigrations(wave, migrations, plan.id).length > 0) return 'complete';
  return isWaveActive(plan, wave.id, migrations) ? 'active' : 'waiting';
}
