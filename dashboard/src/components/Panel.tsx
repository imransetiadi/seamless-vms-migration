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
  /** Makes the panel a place a link lands on (`#id`, SDD §16): scrolled to and focused. */
  id?: string;
}

/** A titled card section; the heading labels the region for assistive technology. */
export function Panel({ title, description, actions, children, className, bodyClassName, level = 2, id }: PanelProps) {
  const headingId = useId();
  const Heading = level === 2 ? 'h2' : 'h3';
  return (
    <section
      id={id}
      tabIndex={id ? -1 : undefined}
      aria-labelledby={headingId}
      className={cn('card flex min-w-0 flex-col', id && 'scroll-mt-20 outline-offset-2 focus:outline-2 focus:outline-ring', className)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-t-xl border-b border-border bg-muted/20 px-4 py-3">
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
