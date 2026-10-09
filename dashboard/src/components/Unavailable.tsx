import { CircleHelp } from 'lucide-react';
import { cn } from '../lib/cn';

/**
 * What a panel shows when the data it is built from could not be loaded: unknown, never its empty
 * state ("No VM is down") or a zero. The page's ErrorBanner names the failure, with Retry (SDD §16).
 */
export function Unavailable({ what, className }: { what: string; className?: string }) {
  return (
    <p className={cn('flex items-center gap-2 py-2 text-sm text-muted-foreground', className)}>
      <CircleHelp aria-hidden className="size-4 shrink-0" />
      Unknown: the {what} could not be loaded.
    </p>
  );
}
