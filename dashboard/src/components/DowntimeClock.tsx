import { CircleCheck, Pause, Timer } from 'lucide-react';
import { Fragment, useId } from 'react';
import { DOWNTIME_PARTS, type DowntimePart, type Estimate, type Strategy, type Timestamp } from '../api/types';
import { cn } from '../lib/cn';
import { SLO_ICONS, sloTone, type SloTone } from '../lib/slo';
import { demoTimeText, speedOf } from '../lib/stats';
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
  /** The estimate's parts (SDD §4, ADV-16): listed under it, those of zero seconds left out. */
  parts?: Estimate['downtime_parts'];
  /** Names the copy part: the final sync of a warm strategy, the volume handover, or the copy. */
  strategy?: Strategy;
  /** `GET /health` `demo_speed`: in demo mode the clock runs in model time, as the passes do (SDD §16, ADV-16). */
  demoSpeed?: number | null;
  className?: string;
}

const PART_LABELS: Record<DowntimePart, string> = {
  shutdown_s: 'Shutdown',
  copy_s: 'Copy',
  conversion_s: 'Conversion',
  create_s: 'Create',
  boot_s: 'Boot',
};

function partLabel(part: DowntimePart, strategy: Strategy | undefined): string {
  if (part !== 'copy_s') return PART_LABELS[part];
  if (strategy === 'warm' || strategy === 'vmware_warm') return 'Final sync';
  return strategy === 'storage_handover' ? 'Volume handover' : PART_LABELS.copy_s;
}

/** The actual downtime against the estimate, in words; under a second apart reads as estimated. */
function comparisonText(actualS: number, estimateS: number): string {
  const diff = actualS - estimateS;
  const verdict = Math.abs(diff) < 1 ? 'as estimated' : `${formatDuration(Math.abs(diff))} ${diff > 0 ? 'over' : 'under'} the estimate`;
  return `Actual ${formatDuration(actualS)}: ${verdict}.`;
}

/**
 * Downtime as a running clock from `downtime_started_at` (role=timer, not a live region, so it does
 * not chatter), frozen at the actual downtime once it ended, always compared with the SLO in words.
 * In demo mode it runs in model time, the time base of the passes and the actual downtime (ADV-16);
 * below it, the estimate's parts and, once it ended, the actual downtime against the estimate.
 */
export function DowntimeClock({
  startedAt,
  endedAt,
  actualS,
  sloS = null,
  estimateS = null,
  parts = {},
  strategy,
  demoSpeed = null,
  className,
}: DowntimeClockProps) {
  const running = Boolean(startedAt) && !endedAt;
  const speed = speedOf(demoSpeed);
  // a fast demo clock moves in smaller steps
  const now = useNow(speed > 1 ? 250 : 1000, running);
  const elapsed = !startedAt
    ? 0
    : endedAt
      ? (actualS ?? (secondsBetween(startedAt, endedAt) ?? 0) * speed)
      : (secondsBetween(startedAt, now) ?? 0) * speed;
  const id = useId();
  const shownParts = DOWNTIME_PARTS.filter((part) => (parts[part] ?? 0) > 0);
  const demoNote = startedAt ? demoTimeText(speed) : null;

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
      {shownParts.length > 0 && (
        <dl aria-label="Estimated downtime, by part" className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-0.5 border-l-2 border-border pl-2.5 text-xs">
          {shownParts.map((part) => (
            <Fragment key={part}>
              <dt id={`${id}-${part}`} className="text-muted-foreground">
                {partLabel(part, strategy)}
              </dt>
              <dd aria-labelledby={`${id}-${part}`} className="num text-right text-foreground">
                {formatDuration(parts[part])}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}
      {endedAt && actualS !== null && estimateS !== null && <p className="text-xs text-foreground">{comparisonText(actualS, estimateS)}</p>}
      {demoNote && <p className="text-xs text-muted-foreground">{demoNote}</p>}
    </div>
  );
}
