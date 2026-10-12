import { CircleAlert, RotateCcw } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ApiError, errorMessage } from '../api/client';
import { cn } from '../lib/cn';
import { Button } from './Button';

export interface ErrorBannerProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
  /** A dialog's request failure, at the end of its scrolling body: scrolled into view when it appears or changes. */
  reveal?: boolean;
}

/** How long Retry stays busy without an answer before it can be pressed again. */
const RETRY_BUSY_MAX_MS = 30_000;

/** Announced error with its cause and a recovery path (retry). */
export function ErrorBanner({ error, title = 'Something went wrong', onRetry, className, reveal = false }: ErrorBannerProps) {
  const code = error instanceof ApiError && error.code ? error.code : null;
  // Retry is busy until an answer comes: another failure ends it, a success removes the banner (SDD §16)
  const [retrying, setRetrying] = useState(false);
  useEffect(() => setRetrying(false), [error]);
  // a Retry that succeeded takes the banner away with the focused button: the focus goes to the page's heading,
  // never to nothing (SDD §16), unless something else took it meanwhile
  const root = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const banner = root.current;
    return () => {
      if (!banner?.contains(document.activeElement)) return;
      queueMicrotask(() => {
        if (document.activeElement && document.activeElement !== document.body) return;
        const heading = document.querySelector<HTMLElement>('main h1') ?? document.querySelector<HTMLElement>('h1');
        if (!heading) return;
        if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
        heading.focus({ preventScroll: true });
      });
    };
  }, []);
  // seen as well as heard (SDD §16); an error a page shows while loading is never scrolled to, so a refetch in the
  // background never moves the page
  useEffect(() => {
    if (reveal) root.current?.scrollIntoView?.({ block: 'nearest' });
  }, [reveal, error]);
  // never stuck busy: a retry that gets no answer at all frees Retry after the API's own bound
  useEffect(() => {
    if (!retrying) return;
    const timer = window.setTimeout(() => setRetrying(false), RETRY_BUSY_MAX_MS);
    return () => window.clearTimeout(timer);
  }, [retrying]);
  return (
    <div
      ref={root}
      role="alert"
      className={cn('flex flex-wrap items-start gap-3 rounded-md border border-status-danger/40 bg-status-danger/10 p-3 text-sm', className)}
    >
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-foreground">{title}</p>
        <p className="wrap-break-word text-muted-foreground">
          {errorMessage(error)}
          {code && <code className="ml-1 text-xs">[{code}]</code>}
        </p>
      </div>
      {onRetry && (
        <Button
          size="sm"
          icon={RotateCcw}
          loading={retrying}
          onClick={() => {
            setRetrying(true);
            onRetry();
          }}
        >
          Retry
        </Button>
      )}
    </div>
  );
}
