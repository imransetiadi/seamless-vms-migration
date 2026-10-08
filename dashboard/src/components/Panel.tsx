import { useId, type ReactNode } from 'react';
import { cn } from '../lib/cn';

export interface PanelProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Heading level inside the page outline (pages use h1 for their title). */
  level?: 2 | 3;
}

/** A titled card section; the heading labels the region for assistive technology. */
export function Panel({ title, description, actions, children, className, bodyClassName, level = 2 }: PanelProps) {
  const headingId = useId();
  const Heading = level === 2 ? 'h2' : 'h3';
  return (
    <section aria-labelledby={headingId} className={cn('card flex min-w-0 flex-col', className)}>
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <Heading id={headingId} className="text-sm font-semibold text-foreground">
            {title}
          </Heading>
          {description && <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className={cn('min-w-0 p-4', bodyClassName)}>{children}</div>
    </section>
  );
}
