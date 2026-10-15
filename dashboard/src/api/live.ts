/**
 * One SSE connection per app (LiveEventsProvider) fans events out to subscribers and keeps the
 * TanStack Query cache fresh: progress events patch cached migrations in place; persisted events
 * invalidate the affected queries in small batches (NFR-08: live updates ≤ 2 s).
 */
import type { Query, QueryClient, QueryKey } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useRef, useSyncExternalStore } from 'react';
import { isSummaryList, summaryOf } from '../lib/summary';
import { endedPassBytes } from '../lib/transfer';
import type { StreamStatus } from './stream';
import { AUDIT_EVENT_KINDS, type Event, type Migration, type MigrationListQuery, type MigrationSummary, type SyncPass } from './types';

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

/** The stream's status alone: what follows it draws again on a status change, not on every event the snapshot times. */
export function useLiveStatus(): LiveStatus {
  const hub = useLiveHub();
  const status = () => hub.getSnapshot().status;
  return useSyncExternalStore(hub.subscribeSnapshot, status, status);
}

/** Calls `listener` for every streamed event (persisted and ephemeral) while mounted. */
export function useLiveEvents(listener: (event: Event) => void): void {
  const hub = useLiveHub();
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => hub.subscribe((event) => ref.current(event)), [hub]);
}

/** A `migration.sync_pass` payload when it is a pass that ended (SDD §4.3), else null. */
function endedPass(data: Record<string, unknown>): SyncPass | null {
  const ok = typeof data.number === 'number' && typeof data.kind === 'string' && typeof data.ended_at === 'string' && typeof data.bytes_transferred === 'number';
  return ok ? (data as unknown as SyncPass) : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** The least time between two refetches after reopenings of the stream (SDD §16). */
export const RECONNECT_REFETCH_MIN_MS = 30_000;

/**
 * A stream status listener that refetches every query when the stream opens again after a drop
 * (SDD §16): a connection that dropped before its first persisted event resumes live-only, so the
 * gap is never replayed, and ephemeral progress never is. The first open refetches nothing. Once per
 * `minIntervalMs` at most: the first reopening refetches at once, and one within that interval of the
 * last refetch is refetched once the interval has passed, so a connection that drops and opens again
 * and again - a proxy that ends each response after a moment - never turns into a refetch storm (at
 * 1,000 VMs a plan, NFR-03, each refetch reads megabytes), while no reopening goes unrefetched as
 * long as the stream lasts. When the stream ends, a refetch it holds is dropped: the window regaining
 * focus reads again as before (§16).
 */
export function refetchAfterReconnect(queryClient: QueryClient, minIntervalMs = RECONNECT_REFETCH_MIN_MS): (status: LiveStatus) => void {
  let opened = false;
  let lastAt: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const refetch = () => {
    timer = null;
    lastAt = Date.now();
    void queryClient.invalidateQueries();
  };
  return (status) => {
    if (status === 'closed' || status === 'unauthorized' || status === 'idle') {
      if (timer) clearTimeout(timer);
      timer = null;
      return;
    }
    if (status !== 'open') return;
    if (opened) {
      const wait = lastAt === null ? 0 : lastAt + minIntervalMs - Date.now();
      if (wait <= 0) refetch();
      else timer ??= setTimeout(refetch, wait);
    }
    opened = true;
  };
}

/**
 * The queries the event stream keeps fresh: each of their changes comes as an event (SDD §16) - the migration lists and
 * pages, the plans, the figures, the providers, the advisor's recommendations, notes and policies, the event lists.
 */
export const STREAM_KEPT: readonly QueryKey[] = [
  ['migrations'],
  ['migration'],
  ['plans'],
  ['stats'],
  ['providers'],
  ['advisor', 'recommendations'],
  ['advisor', 'notes'],
  ['advisor', 'policies'],
  ['events', 'tail'],
];

/**
 * While the stream is open, the window regaining focus reads nothing it keeps fresh (SDD §16): what the events held
 * while the page was hidden ask for is read once, and a list whose changes they brought is not read again (NFR-03).
 * One whose last read failed is still read on focus, as they all are while the stream is not open. Returns how to stop.
 */
export function keepStreamFresh(queryClient: QueryClient, status: () => LiveStatus): () => void {
  const refetchOnWindowFocus = (query: Query) => status() !== 'open' || query.state.status === 'error';
  for (const queryKey of STREAM_KEPT) queryClient.setQueryDefaults(queryKey, { refetchOnWindowFocus });
  return () => {
    for (const queryKey of STREAM_KEPT) queryClient.setQueryDefaults(queryKey, {});
  };
}

/**
 * One flush: the queries it reads again (by hash), those a splice left to that read while it ran, and when it began.
 */
interface Batch {
  refreshed: Set<string>;
  relied: Set<string>;
  at: number;
}

/** The most migrations one batch reads one by one; past it the batch reads the lists and pages again (SDD §16). */
export const MAX_SPLICED = 50;

/** Whether the page is hidden (a background tab), and a way to hear when it is shown again (SDD §16). */
export interface PageVisibility {
  hidden(): boolean;
  /** Calls `listener` when the page is shown again; returns how to stop. */
  onVisible(listener: () => void): () => void;
}

/** The browser's page visibility: `document.visibilityState` and its `visibilitychange` event. */
export const documentVisibility: PageVisibility = {
  hidden: () => typeof document !== 'undefined' && document.visibilityState === 'hidden',
  onVisible: (listener) => {
    if (typeof document === 'undefined') return () => {};
    const onChange = () => {
      if (document.visibilityState !== 'hidden') listener();
    };
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  },
};

/**
 * Creates an invalidator that coalesces query invalidations into one flush per `delayMs`. With `fetchMigration`,
 * `refreshMigration(id)` reads that one migration once per flush and puts it into the cached lists that hold it
 * (SDD §16): at 1,000 VMs a plan a list is megabytes, read again on every event otherwise. While the page is hidden
 * the flush waits, and runs once when the page is shown again (§16).
 */
export function createBatchedInvalidator(
  queryClient: QueryClient,
  delayMs = 400,
  fetchMigration?: (id: string) => Promise<Migration>,
  visibility: PageVisibility = documentVisibility,
) {
  const pending = new Map<string, QueryKey>();
  const migrations = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;
  // a background tab reads nothing again: what waits is flushed once the page is shown (SDD §16)
  const schedule = () => {
    if (!visibility.hidden()) timer ??= setTimeout(flush, delayMs);
  };
  const stopWatching = visibility.onVisible(() => {
    if (!disposed && (pending.size > 0 || migrations.size > 0)) timer ??= setTimeout(flush, 0);
  });
  const invalidate = (queryKey: QueryKey) => {
    if (disposed) return;
    pending.set(JSON.stringify(queryKey), queryKey);
    schedule();
  };
  const splice = async (id: string, read: (id: string) => Promise<Migration>, batch: Batch) => {
    let fresh: Migration;
    try {
      // read outside the migration's own query: one failed read never puts a loaded migration page into a failure;
      // on a failure the page's query reads it again with its own retries, and every list is read again
      fresh = await read(id);
    } catch {
      invalidate(['migrations']);
      invalidate(['migration', id]);
      return;
    }
    if (disposed) return;
    queryClient.setQueryData<Migration>(['migration', id], fresh);
    for (const query of queryClient.getQueryCache().findAll({ queryKey: ['migrations'] })) {
      // the same batch reads it again, after its events (SDD §16): that read shows them while it runs - should it
      // fail, the list is read once more - and once it succeeded; a list it failed to read is treated as before
      if (batch.refreshed.has(query.queryHash)) {
        const { fetchStatus, dataUpdatedAt, errorUpdatedAt } = query.state;
        if (fetchStatus === 'fetching') {
          batch.relied.add(query.queryHash);
          continue;
        }
        if (dataUpdatedAt >= batch.at && errorUpdatedAt < batch.at) continue;
      }
      const list = query.state.data as Migration[] | undefined;
      // no data yet - its load failed, or the first one is under way: read it again, as before the splice (SDD §16)
      if (list === undefined) {
        invalidate(query.queryKey);
        continue;
      }
      const filter = (query.queryKey[1] ?? {}) as MigrationListQuery;
      const index = list.findIndex((m) => m.id === id);
      const narrowed = filter.phase !== undefined || filter.wave_id !== undefined || filter.limit !== undefined || filter.offset !== undefined;
      if (!narrowed && index < 0 && filter.plan_id !== undefined && filter.plan_id !== fresh.plan_id) continue;
      // a summary list takes the migration's summary, as the API builds it (SDD §16)
      const entry = isSummaryList(query.queryKey) ? summaryOf(fresh) : fresh;
      // filtered by phase, wave or page it may gain, lose or shift; a missing member, or a read under way, is read again
      if (narrowed || index < 0 || query.state.fetchStatus === 'fetching') invalidate(query.queryKey);
      else queryClient.setQueryData<(Migration | MigrationSummary)[]>(query.queryKey, list.map((m, i) => (i === index ? entry : m)));
    }
  };
  const flush = () => {
    timer = null;
    // hidden since the timer started: it waits for the page to be shown
    if (visibility.hidden()) return;
    // the queries this batch reads again (the active ones invalidateQueries refetches), filled below before any splice
    // reaches the lists - each awaits its read first
    const batch: Batch = { refreshed: new Set(), relied: new Set(), at: Date.now() };
    let ids = [...migrations];
    migrations.clear();
    if (ids.length > MAX_SPLICED) {
      // more than one by one is worth - a long-hidden page in a busy wave: the lists and pages are read again (SDD §16)
      pending.set(JSON.stringify(['migrations']), ['migrations']);
      pending.set(JSON.stringify(['migration']), ['migration']);
      ids = [];
    }
    // a page that shows neither a migration nor any migration list takes no read (SDD §16): a query is shown while an
    // enabled observer holds it - a disabled one (a closed form, a panel whose plan cannot be edited) or one kept in
    // the cache after its page closed shows nothing
    const lists = queryClient.getQueryCache().findAll({ queryKey: ['migrations'] }).some((query) => query.isActive());
    let unread = false;
    for (const id of ids) {
      const page = queryClient.getQueryCache().find({ queryKey: ['migration', id], exact: true });
      // a read of it under way may have started before the event: it waits for the next batch
      if (queryClient.isFetching({ queryKey: ['migration', id] }) > 0) migrations.add(id);
      else if (!lists && !page?.isActive()) {
        // read by nothing, but marked stale where it is cached: a page that shows it later, Back included, reads it
        // again as it opens instead of showing it as it was before the event (SDD §16)
        if (page) void queryClient.invalidateQueries({ queryKey: ['migration', id], exact: true, refetchType: 'none' });
        unread = true;
      } else if (fetchMigration) void splice(id, fetchMigration, batch);
    }
    // and so are the lists it may be in
    if (unread && queryClient.getQueryCache().findAll({ queryKey: ['migrations'] }).length > 0) {
      void queryClient.invalidateQueries({ queryKey: ['migrations'], refetchType: 'none' });
    }
    const keys = [...pending.entries()];
    pending.clear();
    for (const [id, queryKey] of keys) {
      // a refresh under way is neither cancelled — with events faster than a slow API none would ever finish —
      // nor trusted to show this event, which may have come after its request: the key waits for the next batch,
      // and is refreshed once the running request is done (SDD §16)
      if (queryClient.isFetching({ queryKey }) > 0) pending.set(id, queryKey);
      else {
        const queries = queryClient.getQueryCache().findAll({ queryKey, type: 'active' });
        for (const query of queries) batch.refreshed.add(query.queryHash);
        void queryClient.invalidateQueries({ queryKey }).then(() => {
          for (const query of queries) if (batch.relied.has(query.queryHash) && query.state.status === 'error') invalidate(query.queryKey);
        });
      }
    }
    if (pending.size > 0 || migrations.size > 0) schedule();
  };
  return {
    invalidate,
    refreshMigration(id: string) {
      if (!fetchMigration) {
        invalidate(['migrations']);
        invalidate(['migration', id]);
        return;
      }
      migrations.add(id);
      schedule();
    },
    dispose() {
      disposed = true;
      stopWatching();
      if (timer) clearTimeout(timer);
      timer = null;
      pending.clear();
      migrations.clear();
    },
  };
}

/** A cached list of whole migrations, not of summaries (SDD §16). */
function fullList(query: Query): boolean {
  return !isSummaryList(query.queryKey);
}

/**
 * An updater for the cached migration lists that patches the one migration in a list that holds it. A list that does
 * not hold it gets `undefined`, which TanStack Query takes as "no update": its data, `dataUpdatedAt` and observers are
 * left alone, so nothing reading it draws again on every progress event (SDD §16).
 */
function holding<M extends { id: string }>(id: string, patch: (m: M) => M) {
  return (old: M[] | undefined): M[] | undefined => (old?.some((m) => m.id === id) ? old.map(patch) : undefined);
}

/** Applies one streamed event to the query cache. */
export function applyEventToCache(
  queryClient: QueryClient,
  event: Event,
  invalidate: (queryKey: QueryKey) => void,
  refreshMigration?: (id: string) => void,
): void {
  const kind = event.kind;
  if (kind === 'heartbeat' || kind === 'migration.log') return;

  if (kind === 'migration.progress') {
    const id = event.migration_id;
    if (!id) return;
    // The event carries the running step's {pct, bytes_done, bytes_total} (SDD §4.3); the migration's
    // figures follow the API's rule (§4.2, §16): bytes_transferred is every pass that ended plus the
    // step's bytes_done, and bytes_total stays the disk's used bytes.
    const pct = num(event.data.pct);
    if (pct === null) {
      invalidate(['migration', id]);
      return;
    }
    const done = num(event.data.bytes_done);
    const patch = (m: Migration): Migration =>
      m.id === id ? { ...m, progress_pct: pct, bytes_transferred: done === null ? m.bytes_transferred : endedPassBytes(m) + done } : m;
    queryClient.setQueryData<Migration>(['migration', id], (old) => (old ? patch(old) : old));
    queryClient.setQueriesData<Migration[]>({ queryKey: ['migrations'], predicate: fullList }, holding(id, patch));
    // a summary holds neither the passes nor the bytes: it takes the percentage alone (SDD §16)
    queryClient.setQueriesData<MigrationSummary[]>(
      { queryKey: ['migrations'], predicate: (query) => isSummaryList(query.queryKey) },
      holding(id, (m) => (m.id === id ? { ...m, progress_pct: pct } : m)),
    );
    return;
  }

  if (kind === 'migration.sync_pass' && event.migration_id) {
    // The event's data is the pass that ended (SDD §4.3): add it to the cached passes, so the next
    // step's progress counts on top of it before the refetch below lands (§16).
    const pass = endedPass(event.data);
    const id = event.migration_id;
    if (pass) {
      const patch = (m: Migration): Migration => {
        if (m.id !== id) return m;
        const passes = [...m.sync_passes.filter((p) => p.number !== pass.number), pass].sort((a, b) => a.number - b.number);
        return { ...m, sync_passes: passes, bytes_transferred: endedPassBytes({ ...m, sync_passes: passes }) };
      };
      queryClient.setQueryData<Migration>(['migration', id], (old) => (old ? patch(old) : old));
      // a summary list has no passes: the read the event asks for brings what the pass changed
      queryClient.setQueriesData<Migration[]>({ queryKey: ['migrations'], predicate: fullList }, holding(id, patch));
    }
  }

  if (kind.startsWith('policy.')) {
    // a policy change (SDD §14.5, §14.6): the policies page and the audit lists
    invalidate(['advisor', 'policies']);
    invalidate(['events', 'tail']);
    return;
  }
  if (kind.startsWith('recommendation.') || kind.startsWith('decision.')) {
    // an audit event (SDD §14.5): the feed, a recommendation's detail and the audit lists
    invalidate(['advisor', 'recommendations']);
    invalidate(['events', 'tail']);
    // a decision sets or clears a hold the plan page lists (SDD §14.6), even when the strategy stays
    if (kind.startsWith('decision.') && event.migration_id && refreshMigration) refreshMigration(event.migration_id);
    else {
      if (event.migration_id) invalidate(['migration', event.migration_id]);
      if (kind.startsWith('decision.')) invalidate(['migrations']);
    }
    return;
  }
  // not memory.lesson_saved: a lesson changes no migration, list or figure, and the Events page and the timeline take
  // it from the stream (SDD §16)
  if (kind.startsWith('migration.') || kind.startsWith('advisor.')) {
    // a Force cutover is an audit event too (SDD §5.4, §14.5): the live audit page reads the audit lists
    if ((AUDIT_EVENT_KINDS as readonly string[]).includes(kind)) invalidate(['events', 'tail']);
    // advisor.strategy announces a recorded recommendation (SDD §14.4); a note is the Advisor page's too (§16)
    if (kind.startsWith('advisor.')) {
      invalidate(['advisor', 'recommendations']);
      invalidate(['advisor', 'notes']);
    }
    // one migration that exists already: read it alone and put it into the lists (SDD §16)
    const one = kind !== 'migration.created' ? event.migration_id : null;
    if (one && refreshMigration) refreshMigration(one);
    else {
      invalidate(['migrations']);
      if (event.migration_id) invalidate(['migration', event.migration_id]);
    }
    invalidate(['stats']);
    // the plan list with its plan: their status and migration counts follow the phases (SDD §16)
    if (kind === 'migration.phase' || kind === 'migration.created') invalidate(['plans']);
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
    return;
  }
  // a user added by another admin, or by the CLI (SDD §13.1): the Users page's list; auth.denied changes no list
  if (kind.startsWith('user.')) invalidate(['users']);
}
