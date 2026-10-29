import type { ReactNode } from 'react';
import { cn } from '../lib/cn';

/**
 * A scroll container for wide or tall tables that hold no focusable content: it is a labelled,
 * focusable region so keyboard users can scroll it with the arrow keys (WCAG 2.1.1; axe
 * `scrollable-region-focusable`). Tables with links or controls inside do not need it.
 */
export function ScrollRegion({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    // A focusable region intentionally handles its own scrolling keys.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div role="region" aria-label={label} tabIndex={0} className={cn('table-wrap', className)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget || event.altKey || event.ctrlKey || event.metaKey) return;
        const delta = { ArrowLeft: [-40, 0], ArrowRight: [40, 0], ArrowUp: [0, -40], ArrowDown: [0, 40] }[event.key];
        if (!delta) return;
        event.preventDefault();
        event.currentTarget.scrollBy({ left: delta[0], top: delta[1] });
      }}>
      {children}
    </div>
  );
}
