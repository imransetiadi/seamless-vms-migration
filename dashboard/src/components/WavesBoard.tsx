import { CircleCheck, CircleHelp, CircleSlash, Clock, History, Pause, Play, Rows3, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { Plan, MigrationLike } from '../api/types';
import type { Tone } from '../lib/phase';
import { waveMigrations, waveRolledBack, waveState } from '../lib/waves';
import { EmptyState } from './EmptyState';
import { ProgressBar } from './ProgressBar';
import { PhaseBadge, StatusBadge } from './StatusBadge';

type BoardState = 'complete' | 'rolled_back' | 'cancelled' | 'active' | 'waiting' | 'paused' | 'planned' | 'unknown';

const STATE_META: Record<BoardState, { label: string; tone: Tone; icon: LucideIcon }> = {
  complete: { label: 'Complete', tone: 'success', icon: CircleCheck },
  // ended, so the next wave starts, but migrated nothing: no success (SDD §16)
  rolled_back: { label: 'Rolled back', tone: 'warning', icon: History },
  // every wave of a cancelled plan, or one whose migrations were all cancelled: nothing done, nothing left (SDD §16)
  cancelled: { label: 'Cancelled', tone: 'neutral', icon: CircleSlash },
  active: { label: 'Active', tone: 'progress', icon: Play },
  waiting: { label: 'Waiting', tone: 'neutral', icon: Clock },
  paused: { label: 'Paused', tone: 'warning', icon: Pause },
  planned: { label: 'Planned', tone: 'info', icon: Rows3 },
  unknown: { label: 'Unknown', tone: 'neutral', icon: CircleHelp },
};

/**
 * Waves in order with their dependency, parallelism, completion and member VMs (SDD §9.4).
 * A wave starts once every wave it depends on is complete (completed/finalized/cancelled/rolled back).
 * Its VMs done count as the plan's: completed and finalized of those not cancelled, which are never done (SDD §16).
 * `migrations` is null when they could not be loaded: each wave then keeps what the plan says about it,
 * and its state and progress read as unknown, never as empty (SDD §16) - a cancelled plan's waves still read Cancelled.
 */
export function WavesBoard({ plan, migrations, emptyAction }: { plan: Plan; migrations: readonly MigrationLike[] | null; emptyAction?: ReactNode }) {
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
        const kept = members.filter((m) => m.phase !== 'cancelled');
        const done = kept.filter((m) => m.phase === 'completed' || m.phase === 'finalized').length;
        const computed = migrations ? waveState(plan, wave, migrations) : null;
        const state: BoardState =
          plan.status === 'cancelled' || (members.length > 0 && kept.length === 0)
            ? 'cancelled'
            : !computed
              ? 'unknown'
              : computed === 'complete'
                ? waveRolledBack(members)
                  ? 'rolled_back'
                  : 'complete'
                : plan.derived_status === 'running'
                  ? computed
                  : // a Paused plan's active wave waits as the plan does (SDD §8.1)
                    plan.derived_status === 'paused' && computed === 'active'
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
            {/* a cancelled wave has nothing left to do: no bar, no count (SDD §16) */}
            {state === 'cancelled' ? null : migrations ? (
              <div className="flex items-center gap-2">
                <ProgressBar
                  value={kept.length ? (done / kept.length) * 100 : 0}
                  label={`${wave.name}: VMs done`}
                  tone={state === 'rolled_back' ? 'warning' : 'success'}
                  size="sm"
                />
                <span className="num shrink-0 text-xs text-muted-foreground">
                  {done}/{kept.length}
                </span>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">Progress unknown</p>
            )}
            <ul className="flex flex-col gap-1">
              {members.map((m) => (
                <li key={m.id} className="flex min-w-0 items-center justify-between gap-2 text-sm">
                  <Link to={`/migrations/${m.id}`} className="min-w-0 wrap-anywhere font-mono underline-offset-4 hover:underline">
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
