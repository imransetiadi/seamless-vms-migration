import { cn } from '../lib/cn';

/** Shimmer placeholder that reserves space while data loads (avoids layout shift). */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn('animate-pulse rounded-md bg-muted', className)} />;
}

/** A labelled loading region for screen readers plus visual skeleton rows. */
export function LoadingBlock({ label = 'Loading…', rows = 3, className }: { label?: string; rows?: number; className?: string }) {
  return (
    <div role="status" className={cn('flex flex-col gap-2', className)}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  );
}
