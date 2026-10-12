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
 * The last figure fills the rest of its row: the space after it would read as an empty bordered cell, framed by the
 * outlines around it. Seven figures leave one in a phone's 2 columns and three in 4 from sm, none in 7.
 */
const LAST_SPAN: Record<number, string> = {
  7: 'col-span-2 2xl:col-span-1',
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
      {metrics.map((metric, index) => (
        <div
          key={metric.label}
          role="group"
          aria-label={metric.label}
          // tighter on a phone, so the live panels below start on the first screen; every figure and hint stays
          className={cn(
            'flex min-w-0 flex-col gap-0.5 bg-card px-3 py-2 outline-1 outline-border sm:px-4 sm:py-3',
            index === metrics.length - 1 && LAST_SPAN[metrics.length],
          )}
        >
          <span className="text-xs text-muted-foreground sm:text-sm">{metric.label}</span>
          <span
            className={cn(
              'num text-xl font-semibold leading-tight sm:text-2xl',
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
