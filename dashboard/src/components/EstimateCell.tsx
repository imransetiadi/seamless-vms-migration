import { Ban, CircleCheck, TriangleAlert } from 'lucide-react';
import type { Estimate } from '../api/types';
import { formatDuration } from '../lib/format';

/** Estimated downtime (SDD §9.1) with its SLO verdict in words, plus warm pass count. */
export function EstimateCell({ estimate, compact = false }: { estimate: Estimate | null; compact?: boolean }) {
  if (!estimate) return <span className="text-xs text-muted-foreground">Not estimated</span>;
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="num whitespace-nowrap text-foreground">{formatDuration(estimate.downtime_s)}</span>
      {estimate.meets_slo ? (
        <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-status-success">
          <CircleCheck aria-hidden className="size-3.5 shrink-0" />
          within SLO
        </span>
      ) : (
        <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-status-warning">
          <TriangleAlert aria-hidden className="size-3.5 shrink-0" />
          over SLO
        </span>
      )}
      {!compact && estimate.passes > 0 && (
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {estimate.passes} pass{estimate.passes === 1 ? '' : 'es'} · pre-copy {formatDuration(estimate.precopy_s)}
        </span>
      )}
      {!estimate.eligible && (
        <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-status-danger">
          <Ban aria-hidden className="size-3.5 shrink-0" />
          not eligible
        </span>
      )}
    </div>
  );
}
