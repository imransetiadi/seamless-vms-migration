import { Check } from 'lucide-react';
import type { Migration, Phase } from '../api/types';
import { cn } from '../lib/cn';
import { happyPath, phaseMeta } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';

type StepState = 'done' | 'current' | 'upcoming' | 'interrupted';

const SR_STATE: Record<StepState, string> = {
  done: 'completed',
  current: 'current step',
  upcoming: 'not started',
  interrupted: 'interrupted',
};

export interface PhaseStepperProps {
  migration: Pick<Migration, 'phase' | 'strategy' | 'phase_history'>;
  className?: string;
}

/**
 * The success path for the migration's strategy (SDD §5.2). Exceptional phases (blocked, failed,
 * rolling back, rolled back, cancelled) mark the step where the run diverged and are appended as
 * the current step, so the operator sees both how far it got and where it is now.
 */
export function PhaseStepper({ migration, className }: PhaseStepperProps) {
  const path = happyPath(migration.strategy);
  const onPath = path.includes(migration.phase);
  const visited = new Set<Phase>([...migration.phase_history.map((h) => h.to_phase), migration.phase]);
  const reached = onPath
    ? path.indexOf(migration.phase)
    : path.reduce((last, phase, index) => (visited.has(phase) ? index : last), -1);
  const offPathMeta = onPath ? null : phaseMeta(migration.phase);

  const stateOf = (index: number): StepState => {
    if (onPath) return index < reached ? 'done' : index === reached ? 'current' : 'upcoming';
    return index < reached ? 'done' : index === reached ? 'interrupted' : 'upcoming';
  };

  return (
    <div className={cn('flex flex-col gap-3 md:flex-row md:items-start', className)}>
      <ol aria-label="Migration lifecycle" className="grid flex-1 gap-1 md:auto-cols-fr md:grid-flow-col md:gap-0">
        {path.map((phase, index) => {
          const meta = phaseMeta(phase);
          const state = stateOf(index);
          const Icon = state === 'done' ? Check : meta.icon;
          const toneClasses = TONE_CLASSES[state === 'interrupted' && offPathMeta ? offPathMeta.tone : meta.tone];
          return (
            <li
              key={phase}
              aria-current={state === 'current' ? 'step' : undefined}
              className="relative flex min-w-0 items-center gap-2 md:flex-col md:gap-1 md:px-1 md:text-center"
            >
              {index > 0 && (
                <span
                  aria-hidden
                  className={cn(
                    'absolute hidden h-0.5 md:block',
                    'left-[calc(-50%+16px)] right-[calc(50%+16px)] top-[15px]',
                    index <= reached ? 'bg-status-success/70' : 'bg-border',
                  )}
                />
              )}
              <span
                aria-hidden
                className={cn(
                  'relative z-1 flex size-8 shrink-0 items-center justify-center rounded-full border bg-card transition-colors duration-200',
                  state === 'done' && 'border-status-success/70 text-status-success',
                  state === 'current' && cn('border-2', toneClasses.border, toneClasses.text),
                  state === 'interrupted' && cn('border-2 border-dashed', toneClasses.border, toneClasses.text),
                  state === 'upcoming' && 'border-border text-muted-foreground',
                )}
              >
                <Icon className="size-4" />
              </span>
              <span
                className={cn(
                  'min-w-0 text-xs leading-tight',
                  state === 'current' || state === 'interrupted' ? 'font-semibold text-foreground' : 'text-muted-foreground',
                  state === 'done' && 'text-foreground',
                )}
              >
                {meta.label}
                <span className="sr-only">, {SR_STATE[state]}</span>
              </span>
            </li>
          );
        })}
      </ol>
      {offPathMeta && (
        <p
          aria-current="step"
          className={cn(
            'flex items-center gap-2 self-start rounded-md border px-3 py-2 text-sm font-semibold md:ml-2',
            TONE_CLASSES[offPathMeta.tone].badge,
          )}
        >
          <offPathMeta.icon aria-hidden className="size-4 shrink-0" />
          <span>
            Now: {offPathMeta.label}
            <span className="sr-only"> — {offPathMeta.description}</span>
          </span>
        </p>
      )}
    </div>
  );
}
