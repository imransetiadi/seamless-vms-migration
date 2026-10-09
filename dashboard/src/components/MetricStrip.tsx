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

/**
 * Key numbers in one strip separated by rules (not a row of identical cards): each metric is a
 * labelled group so screen readers announce "Failed, 1".
 */
export function MetricStrip({ metrics, label, className }: { metrics: Metric[]; label: string; className?: string }) {
  return (
    // gap-px over the border colour draws the rules between cells at every breakpoint
    <section aria-label={label} className={cn('grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-3 2xl:grid-cols-6', className)}>
      {metrics.map((metric) => (
        <div
          key={metric.label}
          role="group"
          aria-label={metric.label}
          className="flex min-w-0 flex-col gap-0.5 bg-card px-4 py-3"
        >
          <span className="text-sm text-muted-foreground">{metric.label}</span>
          <span className={cn('num text-2xl font-semibold leading-tight', metric.tone ? TONE_CLASSES[metric.tone].text : 'text-foreground')}>
            {metric.value}
          </span>
          {metric.hint && <span className="text-xs text-muted-foreground">{metric.hint}</span>}
        </div>
      ))}
    </section>
  );
}
