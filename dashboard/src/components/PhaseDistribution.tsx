import { Layers } from 'lucide-react';
import type { Phase } from '../api/types';
import { cn } from '../lib/cn';
import { formatNumber } from '../lib/format';
import { PHASE_ORDER, phaseMeta } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { EmptyState } from './EmptyState';

/**
 * Migrations per phase as labelled horizontal bars in lifecycle order (15 categories is too many
 * for a pie). Each row carries the phase icon, its name and the count — colour is supplementary.
 */
export function PhaseDistribution({ byPhase }: { byPhase: Partial<Record<Phase, number>> }) {
  const rows = PHASE_ORDER.map((phase) => ({ phase, count: byPhase[phase] ?? 0 })).filter((r) => r.count > 0);
  const total = rows.reduce((sum, r) => sum + r.count, 0);
  const max = Math.max(1, ...rows.map((r) => r.count));

  if (rows.length === 0) {
    return <EmptyState icon={Layers} title="No migrations yet" description="Create a plan to see migrations by phase." />;
  }

  return (
    <ul className="flex flex-col gap-1.5" aria-label={`${formatNumber(total)} migrations by phase`}>
      {rows.map(({ phase, count }) => {
        const meta = phaseMeta(phase);
        const tone = TONE_CLASSES[meta.tone];
        return (
          <li key={phase} className="grid grid-cols-[minmax(7.5rem,9.5rem)_minmax(0,1fr)_2.5rem] items-center gap-2 text-sm">
            <span className={cn('flex min-w-0 items-center gap-1.5', tone.text)}>
              <meta.icon aria-hidden className="size-3.5 shrink-0" />
              <span className="truncate text-foreground">{meta.label}</span>
            </span>
            <span aria-hidden className="h-2 overflow-hidden rounded-full bg-muted">
              <span
                className={cn('block h-full w-full origin-left rounded-full transition-transform duration-300', tone.bar)}
                style={{ transform: `scaleX(${count / max})` }}
              />
            </span>
            <span className="num text-right text-foreground">
              {formatNumber(count)}
              <span className="sr-only"> of {formatNumber(total)}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
