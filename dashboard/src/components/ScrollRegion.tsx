import type { ReactNode } from 'react';
import { cn } from '../lib/cn';

/**
 * A scroll container for wide or tall tables that hold no focusable content: it is a labelled,
 * focusable region so keyboard users can scroll it with the arrow keys (WCAG 2.1.1; axe
 * `scrollable-region-focusable`). Tables with links or controls inside do not need it.
 */
export function ScrollRegion({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className={cn('table-wrap', className)}>
      {children}
    </div>
  );
}
