import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '../lib/cn';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';

export interface KpiTileProps {
  label: string;
  value: ReactNode;
  unit?: string;
  hint?: ReactNode;
  icon?: LucideIcon;
  tone?: Tone;
  className?: string;
}

/** A single metric: uppercase label, Fira Code value, optional unit and hint. */
export function KpiTile({ label, value, unit, hint, icon: Icon, tone, className }: KpiTileProps) {
  return (
    <div role="group" aria-label={label} className={cn('card flex min-w-0 flex-col gap-1 p-3', className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
        {Icon && <Icon aria-hidden className={cn('size-4 shrink-0', tone ? TONE_CLASSES[tone].text : 'text-muted-foreground')} />}
      </div>
      <div className="num truncate text-2xl font-semibold leading-tight text-foreground">
        {value}
        {unit && <span className="ml-1 text-sm font-normal text-muted-foreground">{unit}</span>}
      </div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}
