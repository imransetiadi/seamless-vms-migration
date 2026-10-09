import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { useApi } from './hooks';
import { applyEventToCache, createBatchedInvalidator, LiveContext, LiveEventHub, refetchAfterReconnect } from './live';
import { streamEvents } from './stream';

/**
 * Owns the app's single SSE connection while `enabled` (i.e. signed in). The first connection
 * streams live events only; reconnects resume with `since=<last seq>` (SDD §12), and every query
 * is refetched once the stream is open again (SDD §16).
 */
export function LiveEventsProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [hub] = useState(() => new LiveEventHub());

  useEffect(() => {
    if (!enabled) {
      hub.setStatus('idle');
      return;
    }
    const controller = new AbortController();
    const batch = createBatchedInvalidator(queryClient);
    const unsubscribe = hub.subscribe((event) => applyEventToCache(queryClient, event, batch.invalidate));
    const refetch = refetchAfterReconnect(queryClient);
    void streamEvents(null, hub.publish, controller.signal, {
      client: api,
      onStatus: (status) => {
        hub.setStatus(status);
        refetch(status);
      },
      onHeartbeat: hub.heartbeat,
    });
    return () => {
      controller.abort();
      unsubscribe();
      batch.dispose();
    };
  }, [api, enabled, hub, queryClient]);

  return <LiveContext.Provider value={hub}>{children}</LiveContext.Provider>;
}
