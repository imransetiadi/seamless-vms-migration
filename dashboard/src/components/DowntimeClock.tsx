import { CircleCheck, Pause, Timer } from 'lucide-react';
import type { Timestamp } from '../api/types';
import { cn } from '../lib/cn';
import { SLO_ICONS, sloTone, type SloTone } from '../lib/slo';
import { formatClock, formatDuration, secondsBetween } from '../lib/format';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { useNow } from '../lib/useNow';
import { ProgressBar } from './ProgressBar';

export interface DowntimeClockProps {
  /** `Migration.downtime_started_at` — set when the executor stops the source VM (SDD §5.2). */
  startedAt: Timestamp | null;
  endedAt: Timestamp | null;
  actualS: number | null;
  sloS?: number | null;
  estimateS?: number | null;
  className?: string;
}

/**
 * Downtime as a running clock from `downtime_started_at` (role=timer, not a live region, so it does
 * not chatter), frozen at the actual downtime once it ended, always compared with the SLO in words.
 */
export function DowntimeClock({ startedAt, endedAt, actualS, sloS = null, estimateS = null, className }: DowntimeClockProps) {
  const running = Boolean(startedAt) && !endedAt;
  const now = useNow(1000, running);
  const elapsed = !startedAt ? 0 : endedAt ? (actualS ?? secondsBetween(startedAt, endedAt) ?? 0) : (secondsBetween(startedAt, now) ?? 0);

  let state: { text: string; tone: Tone; icon: typeof Timer };
  if (!startedAt) state = { text: 'Not started — the source VM is running', tone: 'neutral', icon: Pause };
  else if (running) state = { text: 'Running — source VM stopped', tone: 'warning', icon: Timer };
  else state = { text: 'Downtime ended', tone: 'success', icon: CircleCheck };

  let slo: { text: string; tone: SloTone } | null = null;
  if (sloS && startedAt) {
    if (elapsed > sloS) slo = { text: `Over the SLO by ${formatClock(elapsed - sloS)}`, tone: 'danger' };
    else if (running) slo = { text: `${formatClock(sloS - elapsed)} left in the SLO`, tone: sloTone(elapsed, sloS) };
    else slo = { text: 'Within the SLO', tone: 'success' };
  }
  const SloIcon = slo ? SLO_ICONS[slo.tone] : null;
  const pct = sloS ? Math.min(100, (elapsed / sloS) * 100) : 0;

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span
          role="timer"
          aria-label="Downtime"
          className={cn(
            'num text-4xl font-semibold leading-none',
            slo?.tone === 'danger' ? 'text-status-danger' : startedAt ? 'text-foreground' : 'text-muted-foreground',
          )}
        >
          {formatClock(elapsed)}
        </span>
        <span className={cn('inline-flex items-center gap-1 text-sm', TONE_CLASSES[state.tone].text)}>
          <state.icon aria-hidden className={cn('size-4 shrink-0', running && 'motion-safe:animate-pulse')} />
          {state.text}
        </span>
      </div>
      {sloS ? (
        <>
          <ProgressBar
            value={pct}
            label="Downtime against the SLO"
            valueText={`${formatClock(elapsed)} of ${formatClock(sloS)}`}
            tone={slo?.tone === 'danger' ? 'danger' : slo?.tone === 'warning' ? 'warning' : startedAt ? 'success' : 'neutral'}
            size="sm"
          />
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>SLO {formatDuration(sloS)}</span>
            {estimateS !== null && <span>Estimated {formatDuration(estimateS)}</span>}
            {slo && (
              <span className={cn('inline-flex items-center gap-1 font-medium', TONE_CLASSES[slo.tone].text)}>
                {SloIcon && <SloIcon aria-hidden className="size-3.5" />}
                {slo.text}
              </span>
            )}
          </p>
        </>
      ) : (
        estimateS !== null && <p className="text-xs text-muted-foreground">Estimated {formatDuration(estimateS)}</p>
      )}
    </div>
  );
}
