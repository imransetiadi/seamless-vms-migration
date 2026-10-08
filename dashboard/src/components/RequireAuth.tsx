import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { ApiError } from '../api/client';
import { useMe } from '../api/hooks';
import { LiveEventsProvider } from '../api/LiveEventsProvider';
import { ErrorBanner } from './ErrorBanner';
import { LoadingBlock } from './Skeleton';

/**
 * Gate for every page except /login: resolves the principal with GET /me (which also covers
 * deployments with authentication disabled) and owns the live event stream while signed in.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const location = useLocation();

  if (me.isPending) {
    return (
      <div className="mx-auto max-w-md p-8">
        <LoadingBlock label="Checking your session…" rows={2} />
      </div>
    );
  }
  if (me.error instanceof ApiError && me.error.status === 401) {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
  }
  if (me.error) {
    return (
      <div className="mx-auto max-w-xl p-8">
        <ErrorBanner error={me.error} title="Cannot reach the Seamless control plane" onRetry={() => void me.refetch()} />
      </div>
    );
  }
  return <LiveEventsProvider enabled>{children}</LiveEventsProvider>;
}
