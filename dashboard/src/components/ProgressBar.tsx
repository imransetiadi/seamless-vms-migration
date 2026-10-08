import { cn } from '../lib/cn';
import { formatPct } from '../lib/format';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';

export interface ProgressBarProps {
  value: number;
  label: string;
  tone?: Tone;
  /** Visible text; defaults to the formatted percentage. */
  valueText?: string;
  size?: 'sm' | 'md';
  className?: string;
}

/** Determinate progress (role=progressbar). The fill animates with transform only. */
export function ProgressBar({ value, label, tone = 'progress', valueText, size = 'md', className }: ProgressBarProps) {
  const pct = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
  const text = valueText ?? formatPct(pct);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct * 10) / 10}
      aria-valuetext={text}
      className={cn('w-full overflow-hidden rounded-full bg-muted', size === 'sm' ? 'h-1.5' : 'h-2.5', className)}
    >
      <div
        className={cn('h-full w-full origin-left rounded-full transition-transform duration-300 ease-out', TONE_CLASSES[tone].bar)}
        style={{ transform: `scaleX(${pct / 100})` }}
      />
    </div>
  );
}
