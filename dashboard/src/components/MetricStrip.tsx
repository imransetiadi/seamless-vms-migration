import type { ReactNode } from 'react';
import { cn } from '../lib/cn';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';

export interface Metric {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  /** Colours the value only when the number itself is the signal (failures, compliance). */
  tone?: Tone;
}

/** Columns per breakpoint by the number of metrics: one row on wide screens. */
const COLUMNS: Record<number, string> = {
  6: 'grid-cols-2 sm:grid-cols-3 2xl:grid-cols-6',
  7: 'grid-cols-2 sm:grid-cols-4 2xl:grid-cols-7',
};

/**
 * Key numbers in one strip separated by rules (not a row of identical cards): each metric is a
 * labelled group so screen readers announce "Failed, 1". While newer figures load (`busy`) the strip is
 * `aria-busy` and its values turn to the muted foreground: still AA contrast, never faded (SDD §16).
 */
export function MetricStrip({ metrics, label, busy = false, className }: { metrics: Metric[]; label: string; busy?: boolean; className?: string }) {
  return (
    // each cell's outline fills the 1px gap beside it: the rules run between cells at every breakpoint,
    // and a last row the cells do not fill stays the card's colour
    <section
      aria-label={label}
      aria-busy={busy || undefined}
      className={cn('grid gap-px overflow-hidden rounded-lg border border-border bg-card', COLUMNS[metrics.length] ?? COLUMNS[6], className)}
    >
      {metrics.map((metric) => (
        <div
          key={metric.label}
          role="group"
          aria-label={metric.label}
          className="flex min-w-0 flex-col gap-0.5 bg-card px-4 py-3 outline-1 outline-border"
        >
          <span className="text-sm text-muted-foreground">{metric.label}</span>
          <span
            className={cn(
              'num text-2xl font-semibold leading-tight',
              busy ? 'text-muted-foreground' : metric.tone ? TONE_CLASSES[metric.tone].text : 'text-foreground',
            )}
          >
            {metric.value}
          </span>
          {metric.hint && <span className="text-xs text-muted-foreground">{metric.hint}</span>}
        </div>
      ))}
    </section>
  );
}
