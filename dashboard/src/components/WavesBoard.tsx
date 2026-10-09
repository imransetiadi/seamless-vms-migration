import { CircleCheck, CircleHelp, Clock, Pause, Play, Rows3, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { Migration, Plan } from '../api/types';
import type { Tone } from '../lib/phase';
import { WAVE_COMPLETE_PHASES, waveMigrations, waveState } from '../lib/waves';
import { EmptyState } from './EmptyState';
import { ProgressBar } from './ProgressBar';
import { PhaseBadge, StatusBadge } from './StatusBadge';

type BoardState = 'complete' | 'active' | 'waiting' | 'paused' | 'planned' | 'unknown';

const STATE_META: Record<BoardState, { label: string; tone: Tone; icon: LucideIcon }> = {
  complete: { label: 'Complete', tone: 'success', icon: CircleCheck },
  active: { label: 'Active', tone: 'progress', icon: Play },
  waiting: { label: 'Waiting', tone: 'neutral', icon: Clock },
  paused: { label: 'Paused', tone: 'warning', icon: Pause },
  planned: { label: 'Planned', tone: 'info', icon: Rows3 },
  unknown: { label: 'Unknown', tone: 'neutral', icon: CircleHelp },
};

/**
 * Waves in order with their dependency, parallelism, completion and member VMs (SDD §9.4).
 * A wave starts once every wave it depends on is complete (completed/finalized/cancelled/rolled back).
 * `migrations` is null when they could not be loaded: each wave then keeps what the plan says about it,
 * and its state and progress read as unknown, never as empty (SDD §16).
 */
export function WavesBoard({ plan, migrations, emptyAction }: { plan: Plan; migrations: Migration[] | null; emptyAction?: ReactNode }) {
  const waves = [...plan.waves].sort((a, b) => a.order - b.order);
  if (waves.length === 0) {
    return (
      <EmptyState
        icon={Rows3}
        title="No waves yet"
        description="Auto-plan waves to run a low-risk pilot first and keep application VMs together."
        action={emptyAction}
      />
    );
  }
  const names = new Map(waves.map((w) => [w.id, w.name]));

  return (
    <ol className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
      {waves.map((wave) => {
        const members = migrations ? waveMigrations(wave, migrations, plan.id) : [];
        const done = members.filter((m) => WAVE_COMPLETE_PHASES.has(m.phase)).length;
        const computed = migrations ? waveState(plan, wave, migrations) : null;
        const state: BoardState = !computed
          ? 'unknown'
          : computed === 'complete'
            ? 'complete'
            : plan.status === 'running'
              ? computed
              : plan.status === 'paused' && computed === 'active'
                ? 'paused'
                : 'planned';
        const meta = STATE_META[state];
        return (
          <li key={wave.id} className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-background/40 p-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  Wave {wave.order} · <code>{wave.id}</code>
                </p>
                <h3 className="wrap-break-word text-sm font-semibold text-foreground">{wave.name}</h3>
              </div>
              <StatusBadge tone={meta.tone} icon={meta.icon} label={meta.label} />
            </div>
            <p className="text-xs text-muted-foreground">
              {wave.depends_on.length ? `After ${wave.depends_on.map((d) => names.get(d) ?? d).join(', ')}` : 'No dependencies'} · up to{' '}
              {wave.max_parallel} in parallel
            </p>
            {migrations ? (
              <div className="flex items-center gap-2">
                <ProgressBar value={members.length ? (done / members.length) * 100 : 0} label={`${wave.name}: VMs done`} tone="success" size="sm" />
                <span className="num shrink-0 text-xs text-muted-foreground">
                  {done}/{members.length}
                </span>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">Progress unknown</p>
            )}
            <ul className="flex flex-col gap-1">
              {members.map((m) => (
                <li key={m.id} className="flex min-w-0 items-center justify-between gap-2 text-sm">
                  <Link to={`/migrations/${m.id}`} className="min-w-0 truncate font-mono underline-offset-4 hover:underline">
                    {m.vm.name}
                  </Link>
                  <PhaseBadge phase={m.phase} />
                </li>
              ))}
            </ul>
          </li>
        );
      })}
    </ol>
  );
}
