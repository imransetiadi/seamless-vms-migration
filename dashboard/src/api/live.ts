/**
 * One SSE connection per app (LiveEventsProvider) fans events out to subscribers and keeps the
 * TanStack Query cache fresh: progress events patch cached migrations in place; persisted events
 * invalidate the affected queries in small batches (NFR-08: live updates ≤ 2 s).
 */
import type { QueryClient, QueryKey } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useRef, useSyncExternalStore } from 'react';
import type { StreamStatus } from './stream';
import type { Event, Migration } from './types';

export type LiveStatus = StreamStatus | 'idle';

export interface LiveSnapshot {
  status: LiveStatus;
  lastEventAt: number | null;
  lastHeartbeatAt: number | null;
}

export class LiveEventHub {
  private readonly listeners = new Set<(event: Event) => void>();
  private readonly snapshotListeners = new Set<() => void>();
  private snapshot: LiveSnapshot = { status: 'idle', lastEventAt: null, lastHeartbeatAt: null };

  publish = (event: Event): void => {
    this.update({ lastEventAt: Date.now() });
    for (const listener of this.listeners) listener(event);
  };

  setStatus = (status: LiveStatus): void => {
    if (status !== this.snapshot.status) this.update({ status });
  };

  heartbeat = (): void => {
    this.update({ lastHeartbeatAt: Date.now() });
  };

  subscribe = (listener: (event: Event) => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  subscribeSnapshot = (listener: () => void): (() => void) => {
    this.snapshotListeners.add(listener);
    return () => this.snapshotListeners.delete(listener);
  };

  getSnapshot = (): LiveSnapshot => this.snapshot;

  private update(patch: Partial<LiveSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.snapshotListeners) listener();
  }
}

export const LiveContext = createContext<LiveEventHub | null>(null);

const idleHub = new LiveEventHub();

export function useLiveHub(): LiveEventHub {
  return useContext(LiveContext) ?? idleHub;
}

export function useLiveSnapshot(): LiveSnapshot {
  const hub = useLiveHub();
  return useSyncExternalStore(hub.subscribeSnapshot, hub.getSnapshot, hub.getSnapshot);
}

/** Calls `listener` for every streamed event (persisted and ephemeral) while mounted. */
export function useLiveEvents(listener: (event: Event) => void): void {
  const hub = useLiveHub();
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => hub.subscribe((event) => ref.current(event)), [hub]);
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * A stream status listener that refetches every query when the stream opens again after a drop
 * (SDD §16): a connection that dropped before its first persisted event resumes live-only, so the
 * gap is never replayed, and ephemeral progress never is. The first open refetches nothing.
 */
export function refetchAfterReconnect(queryClient: QueryClient): (status: LiveStatus) => void {
  let opened = false;
  return (status) => {
    if (status !== 'open') return;
    if (opened) void queryClient.invalidateQueries();
    opened = true;
  };
}

/** Creates an invalidator that coalesces query invalidations into one flush per `delayMs`. */
export function createBatchedInvalidator(queryClient: QueryClient, delayMs = 400) {
  const pending = new Map<string, QueryKey>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  const flush = () => {
    timer = null;
    const keys = [...pending.values()];
    pending.clear();
    for (const queryKey of keys) void queryClient.invalidateQueries({ queryKey });
  };
  return {
    invalidate(queryKey: QueryKey) {
      pending.set(JSON.stringify(queryKey), queryKey);
      timer ??= setTimeout(flush, delayMs);
    },
    dispose() {
      if (timer) clearTimeout(timer);
      timer = null;
      pending.clear();
    },
  };
}

/** Applies one streamed event to the query cache. */
export function applyEventToCache(
  queryClient: QueryClient,
  event: Event,
  invalidate: (queryKey: QueryKey) => void,
): void {
  const kind = event.kind;
  if (kind === 'heartbeat' || kind === 'migration.log') return;

  if (kind === 'migration.progress') {
    const id = event.migration_id;
    if (!id) return;
    // Tolerant reader: `progress_pct`/`bytes_transferred` (Migration field names) or the executor's
    // report_progress(pct, bytes_done, bytes_total) names (SDD §7.1).
    const pct = num(event.data.progress_pct) ?? num(event.data.pct);
    if (pct === null) {
      invalidate(['migration', id]);
      return;
    }
    const done = num(event.data.bytes_transferred) ?? num(event.data.bytes_done);
    const total = num(event.data.bytes_total);
    const patch = (m: Migration): Migration =>
      m.id === id ? { ...m, progress_pct: pct, bytes_transferred: done ?? m.bytes_transferred, bytes_total: total ?? m.bytes_total } : m;
    queryClient.setQueryData<Migration>(['migration', id], (old) => (old ? patch(old) : old));
    queryClient.setQueriesData<Migration[]>({ queryKey: ['migrations'] }, (old) => old?.map(patch));
    return;
  }

  if (kind.startsWith('migration.') || kind.startsWith('advisor.') || kind === 'memory.lesson_saved') {
    invalidate(['migrations']);
    invalidate(['stats']);
    if (event.migration_id) invalidate(['migration', event.migration_id]);
    if (event.plan_id && (kind === 'migration.phase' || kind === 'migration.created')) invalidate(['plans', event.plan_id]);
    return;
  }
  if (kind.startsWith('plan.') || kind.startsWith('wave.')) {
    invalidate(['plans']);
    invalidate(['migrations']);
    invalidate(['stats']);
    return;
  }
  if (kind.startsWith('provider.')) {
    invalidate(['providers']);
    // new endpoint or credentials (or a deleted provider): the cached inventory came from the old ones
    const providerId = event.data.provider_id;
    if (kind !== 'provider.checked' && kind !== 'provider.created' && typeof providerId === 'string') {
      invalidate(['inventory', providerId]);
    }
  }
}
