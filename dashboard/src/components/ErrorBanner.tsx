import { CircleAlert, RotateCcw } from 'lucide-react';
import { ApiError, errorMessage } from '../api/client';
import { cn } from '../lib/cn';
import { Button } from './Button';

export interface ErrorBannerProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}

/** Announced error with its cause and a recovery path (retry). */
export function ErrorBanner({ error, title = 'Something went wrong', onRetry, className }: ErrorBannerProps) {
  const code = error instanceof ApiError && error.code ? error.code : null;
  return (
    <div
      role="alert"
      className={cn('flex flex-wrap items-start gap-3 rounded-md border border-status-danger/40 bg-status-danger/10 p-3 text-sm', className)}
    >
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground">{title}</p>
        <p className="break-words text-muted-foreground">
          {errorMessage(error)}
          {code && <code className="ml-1 text-xs">[{code}]</code>}
        </p>
      </div>
      {onRetry && (
        <Button size="sm" icon={RotateCcw} onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
