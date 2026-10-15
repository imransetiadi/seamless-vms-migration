import { focusManager, QueryClient, QueryObserver, type QueryKey } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import { applyEventToCache, createBatchedInvalidator, documentVisibility, keepStreamFresh, refetchAfterReconnect, type LiveStatus } from './live';
import { buildFixtures } from './mockData';
import { summaryOf } from '../lib/summary';
import { AUDIT_EVENT_KINDS, type Event, type Migration, type MigrationSummary } from './types';

const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
const migration = fx.migrations.find((m) => m.phase === 'precopy') as Migration;

function progressEvent(data: Record<string, unknown>): Event {
  return {
    seq: 0,
    ts: '2026-10-08T12:00:01Z',
    kind: 'migration.progress',
    plan_id: migration.plan_id,
    migration_id: migration.id,
    actor: 'orchestrator',
    message: '',
    data,
  };
}

function setup(m: Migration = migration) {
  const queryClient = new QueryClient();
  queryClient.setQueryData(['migration', m.id], m);
  queryClient.setQueryData(['migrations', { plan_id: m.plan_id }], [m]);
  const invalidate = vi.fn();
  return { queryClient, invalidate };
}

/** Holds `queryKey` with an enabled observer, as a page that shows it does; its data stays as it is (SDD §16). */
function heldByPage(queryClient: QueryClient, queryKey: readonly unknown[]): () => void {
  const observer = new QueryObserver(queryClient, { queryKey, queryFn: async () => queryClient.getQueryData(queryKey) ?? null, staleTime: Infinity });
  return observer.subscribe(() => {});
}

const MiB = 2 ** 20;
const GiB = 2 ** 30;

describe('applyEventToCache', () => {
  it('sets the step percentage and counts the step bytes on top of the passes that ended, like the API (SDD §4.3, §16)', () => {
    const pass = { started_at: '2026-10-08T10:00:00Z', ended_at: '2026-10-08T10:30:00Z', bytes_scanned: 0, bytes_changed: 0, duration_s: 1800 };
    const m: Migration = {
      ...migration,
      bytes_total: 110 * GiB,
      bytes_transferred: 107 * GiB,
      sync_bytes_dropped: 5 * GiB,
      sync_passes: [
        { ...pass, number: 1, kind: 'full', bytes_transferred: 100 * GiB },
        { ...pass, number: 2, kind: 'delta', bytes_transferred: 2 * GiB },
        // a running pass, as the mock lists one: its bytes arrive as bytes_done
        { ...pass, number: 3, kind: 'delta', ended_at: null, duration_s: null, bytes_transferred: 512 * MiB },
      ],
    };
    const { queryClient, invalidate } = setup(m);
    applyEventToCache(queryClient, progressEvent({ pct: 40, bytes_done: 800 * MiB, bytes_total: 2 * GiB, phase: 'syncing' }), invalidate);
    const expected = { progress_pct: 40, bytes_transferred: 107 * GiB + 800 * MiB, bytes_total: 110 * GiB };
    expect(queryClient.getQueryData<Migration>(['migration', m.id])).toMatchObject(expected);
    expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: m.plan_id }])?.[0]).toMatchObject(expected);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('leaves a cached list that does not hold the migration untouched, so nothing reading it is told of a change (SDD §16, perf PF-05)', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(Date.parse('2026-10-08T12:00:00Z'));
      const { queryClient, invalidate } = setup();
      const other = fx.migrations.find((m) => m.plan_id !== migration.plan_id)!;
      const elsewhere = [other];
      queryClient.setQueryData(['migrations', { plan_id: other.plan_id }], elsewhere);
      // a list of every plan holds it among others
      queryClient.setQueryData(['migrations', {}], [other, migration]);
      const before = queryClient.getQueryState(['migrations', { plan_id: other.plan_id }])!.dataUpdatedAt;
      const notified = vi.fn();
      const observer = new QueryObserver(queryClient, { queryKey: ['migrations', { plan_id: other.plan_id }], enabled: false, notifyOnChangeProps: 'all' });
      const unsubscribe = observer.subscribe(notified);
      vi.setSystemTime(Date.parse('2026-10-08T12:00:05Z'));
      applyEventToCache(queryClient, progressEvent({ pct: 41, bytes_done: MiB }), invalidate);
      // the event's data is the pass that ended (SDD §4.3)
      const ended = { number: 9, kind: 'delta', started_at: '2026-10-08T12:00:00Z', ended_at: '2026-10-08T12:00:04Z', bytes_scanned: 0, bytes_changed: 0, bytes_transferred: MiB, duration_s: 4 };
      applyEventToCache(queryClient, { ...progressEvent(ended), seq: 60, kind: 'migration.sync_pass' }, invalidate);
      expect(queryClient.getQueryData(['migrations', { plan_id: other.plan_id }])).toBe(elsewhere);
      expect(queryClient.getQueryState(['migrations', { plan_id: other.plan_id }])!.dataUpdatedAt).toBe(before);
      expect(notified).not.toHaveBeenCalled();
      // the lists that hold it are patched, the others in them kept as they are
      expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: migration.plan_id }])?.[0]?.progress_pct).toBe(41);
      const all = queryClient.getQueryData<Migration[]>(['migrations', {}])!;
      expect(all[1]?.progress_pct).toBe(41);
      expect(all[1]?.sync_passes.some((p) => p.number === 9)).toBe(true);
      expect(all[0]).toBe(other);
      unsubscribe();
    } finally {
      vi.useRealTimers();
    }
  });

  it('adds the pass a sync_pass event carries, so the next step does not drop the transferred figure (SDD §4.3, §16)', () => {
    const base = { started_at: '2026-10-08T10:00:00Z', bytes_scanned: 0, bytes_changed: 0 };
    const m: Migration = {
      ...migration,
      sync_bytes_dropped: 0,
      bytes_transferred: 100 * GiB + 1900 * MiB,
      sync_passes: [
        { ...base, number: 1, kind: 'full', ended_at: '2026-10-08T10:30:00Z', duration_s: 1800, bytes_transferred: 100 * GiB },
        // the running pass as the mock lists it (the API lists none)
        { ...base, number: 2, kind: 'delta', ended_at: null, duration_s: null, bytes_transferred: 1900 * MiB },
      ],
    };
    const { queryClient, invalidate } = setup(m);
    const ended = { ...base, number: 2, kind: 'delta', ended_at: '2026-10-08T11:00:00Z', duration_s: 300, bytes_transferred: 2 * GiB };
    applyEventToCache(queryClient, { ...progressEvent(ended), seq: 50, kind: 'migration.sync_pass' }, invalidate);
    const cached = () => queryClient.getQueryData<Migration>(['migration', m.id])!;
    expect(cached().sync_passes.map((p) => [p.number, p.ended_at])).toEqual([
      [1, '2026-10-08T10:30:00Z'],
      [2, '2026-10-08T11:00:00Z'],
    ]);
    expect(cached().bytes_transferred).toBe(102 * GiB);
    // the refetch stays authoritative
    expect(invalidate).toHaveBeenCalledWith(['migration', m.id]);
    // the next step's first progress counts on top of the pass that ended, in the lists too
    applyEventToCache(queryClient, progressEvent({ pct: 1, bytes_done: 10 * MiB }), invalidate);
    expect(cached().bytes_transferred).toBe(102 * GiB + 10 * MiB);
    expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: m.plan_id }])?.[0]?.bytes_transferred).toBe(102 * GiB + 10 * MiB);
  });

  it('appends the ended pass when the page lists no running pass, as with the API', () => {
    const base = { started_at: '2026-10-08T10:00:00Z', bytes_scanned: 0, bytes_changed: 0, duration_s: 600 };
    const m: Migration = {
      ...migration,
      sync_bytes_dropped: 3 * GiB,
      bytes_transferred: 103 * GiB,
      sync_passes: [{ ...base, number: 7, kind: 'delta', ended_at: '2026-10-08T10:30:00Z', bytes_transferred: 100 * GiB }],
    };
    const { queryClient, invalidate } = setup(m);
    const ended = { ...base, number: 8, kind: 'delta', ended_at: '2026-10-08T10:45:00Z', bytes_transferred: GiB };
    applyEventToCache(queryClient, { ...progressEvent(ended), seq: 51, kind: 'migration.sync_pass' }, invalidate);
    const cached = queryClient.getQueryData<Migration>(['migration', m.id])!;
    expect(cached.sync_passes.map((p) => p.number)).toEqual([7, 8]);
    expect(cached.bytes_transferred).toBe(104 * GiB);
  });

  it('leaves the passes alone when a sync_pass payload is not a pass that ended', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({ pass: { number: 9 } }), seq: 52, kind: 'migration.sync_pass' }, invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toEqual(migration);
    expect(invalidate).toHaveBeenCalledWith(['migration', migration.id]);
  });

  it('keeps the transferred bytes and the disk size when a progress event has no bytes_done', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, progressEvent({ pct: 12.5 }), invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toMatchObject({
      progress_pct: 12.5,
      bytes_transferred: migration.bytes_transferred,
      bytes_total: migration.bytes_total,
    });
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('invalidates instead of guessing when a progress payload has no pct (SDD §4.3)', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, progressEvent({ note: 'unknown shape' }), invalidate);
    // a Migration-field payload is not the event's shape either
    applyEventToCache(queryClient, progressEvent({ progress_pct: 55.5, bytes_transferred: 123 }), invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toMatchObject({
      progress_pct: migration.progress_pct,
      bytes_transferred: migration.bytes_transferred,
    });
    expect(invalidate).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith(['migration', migration.id]);
  });

  it('invalidates the affected queries for persisted events', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 42, kind: 'migration.phase' }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['migrations']);
    expect(invalidate).toHaveBeenCalledWith(['migration', migration.id]);
    // the plan list with its plan: their status and migration counts follow the phases (SDD §16)
    expect(invalidate).toHaveBeenCalledWith(['plans']);
    expect(invalidate).toHaveBeenCalledWith(['stats']);

    invalidate.mockClear();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 43, kind: 'provider.checked', migration_id: null, plan_id: null }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['providers']);
  });

  it('refreshes the recommendations and the audit lists on an audit event (SDD §14.5)', () => {
    for (const kind of ['recommendation.created', 'decision.overridden']) {
      const { queryClient, invalidate } = setup();
      applyEventToCache(queryClient, { ...progressEvent({ target_id: 'rec-000000000001' }), seq: 46, kind }, invalidate);
      expect(invalidate, kind).toHaveBeenCalledWith(['advisor', 'recommendations']);
      expect(invalidate, kind).toHaveBeenCalledWith(['events', 'tail']);
      expect(invalidate, kind).toHaveBeenCalledWith(['migration', migration.id]);
    }
  });

  it.each(AUDIT_EVENT_KINDS)('refreshes the audit lists on %s, which the live audit page shows (SDD §14.5, §16)', (kind) => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({ target_id: migration.id }), seq: 50, kind }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['events', 'tail']);
  });

  it('still reads the forced migration again on a Force cutover, an audit event too (SDD §5.4, §14.5)', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({ target_id: migration.id }), seq: 51, kind: 'migration.force_cutover' }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['events', 'tail']);
    expect(invalidate).toHaveBeenCalledWith(['migration', migration.id]);
    expect(invalidate).toHaveBeenCalledWith(['stats']);
  });

  it("refreshes the migrations lists on a decision: approving clears a hold another viewer's plan page shows (SDD §14.6)", () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({ target_id: 'rec-000000000001' }), seq: 47, kind: 'decision.approved' }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['migrations']);
    invalidate.mockClear();
    // a recommendation alone holds nothing: its decision event follows it
    applyEventToCache(queryClient, { ...progressEvent({ target_id: 'rec-000000000001' }), seq: 48, kind: 'recommendation.created' }, invalidate);
    expect(invalidate).not.toHaveBeenCalledWith(['migrations']);
  });

  it('refreshes the policies and the audit lists on a policy change (SDD §14.6)', () => {
    for (const kind of ['policy.created', 'policy.updated', 'policy.deleted']) {
      const { queryClient, invalidate } = setup();
      applyEventToCache(queryClient, { ...progressEvent({ target_id: 'pol-000000000001' }), seq: 49, kind, migration_id: null, plan_id: null }, invalidate);
      expect(invalidate.mock.calls.map(([key]) => key), kind).toEqual([
        ['advisor', 'policies'],
        ['events', 'tail'],
      ]);
    }
  });

  it('refreshes the recommendations when an advisor event says one was recorded, and only then (SDD §14.4)', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, { ...progressEvent({ recommendation_id: 'rec-000000000001' }), seq: 44, kind: 'advisor.strategy' }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['advisor', 'recommendations']);
    expect(invalidate).toHaveBeenCalledWith(['migration', migration.id]);

    invalidate.mockClear();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 45, kind: 'migration.phase' }, invalidate);
    expect(invalidate).not.toHaveBeenCalledWith(['advisor', 'recommendations']);
  });

  it('patches a summary list with the percentage alone, and leaves it out of a pass (SDD §16, perf audit PF-02)', () => {
    const { queryClient, invalidate } = setup();
    const key = ['migrations', { view: 'summary' }];
    queryClient.setQueryData(key, [summaryOf(migration)]);
    applyEventToCache(queryClient, progressEvent({ pct: 41, bytes_done: MiB }), invalidate);
    expect(queryClient.getQueryData<MigrationSummary[]>(key)).toEqual([{ ...summaryOf(migration), progress_pct: 41 }]);
    // the full list takes the bytes too
    expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: migration.plan_id }])?.[0]?.progress_pct).toBe(41);
    const before = queryClient.getQueryData(key);
    const ended = { number: 9, kind: 'delta', started_at: '2026-10-08T12:00:00Z', ended_at: '2026-10-08T12:00:04Z', bytes_scanned: 0, bytes_changed: 0, bytes_transferred: MiB, duration_s: 4 };
    applyEventToCache(queryClient, { ...progressEvent(ended), seq: 60, kind: 'migration.sync_pass' }, invalidate);
    // a summary holds no passes: the next read brings what the pass changed
    expect(queryClient.getQueryData(key)).toBe(before);
    expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: migration.plan_id }])?.[0]?.sync_passes.some((p) => p.number === 9)).toBe(true);
  });

  it('refreshes the advisor notes on an advisor event, and only then (SDD §16, perf audit PF-02)', () => {
    for (const kind of ['advisor.classification', 'advisor.verification', 'advisor.similar_incidents', 'advisor.strategy']) {
      const { queryClient, invalidate } = setup();
      applyEventToCache(queryClient, { ...progressEvent({}), seq: 46, kind }, invalidate);
      expect(invalidate.mock.calls.map(([key]) => key), kind).toContainEqual(['advisor', 'notes']);
    }
    const { queryClient, invalidate } = setup();
    for (const kind of ['migration.phase', 'migration.error', 'decision.approved', 'recommendation.created', 'plan.updated']) {
      applyEventToCache(queryClient, { ...progressEvent({}), seq: 47, kind }, invalidate);
    }
    expect(invalidate.mock.calls.map(([key]) => key)).not.toContainEqual(['advisor', 'notes']);
  });
});

describe('applyEventToCache for providers', () => {
  it('refreshes the inventory of a provider whose connection details changed', () => {
    const { queryClient, invalidate } = setup();
    for (const kind of ['provider.updated', 'provider.credentials_updated', 'provider.deleted'] as const) {
      invalidate.mockClear();
      applyEventToCache(
        queryClient,
        { ...progressEvent({ provider_id: 'rhosp-dc1' }), seq: 44, kind, migration_id: null, plan_id: null },
        invalidate,
      );
      expect(invalidate).toHaveBeenCalledWith(['providers']);
      expect(invalidate).toHaveBeenCalledWith(['inventory', 'rhosp-dc1']);
    }
  });

  it('leaves the inventory alone for a health check', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(
      queryClient,
      { ...progressEvent({ provider_id: 'rhosp-dc1' }), seq: 45, kind: 'provider.checked', migration_id: null, plan_id: null },
      invalidate,
    );
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith(['providers']);
  });
});

describe('createBatchedInvalidator', () => {
  function batched(delayMs = 400) {
    const queryClient = new QueryClient();
    const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
    return { spy, batch: createBatchedInvalidator(queryClient, delayMs) };
  }

  it('coalesces a burst of events into one invalidation per key after the delay', () => {
    vi.useFakeTimers();
    try {
      const { spy, batch } = batched();
      batch.invalidate(['migrations']);
      batch.invalidate(['stats']);
      batch.invalidate(['migrations']);
      batch.invalidate(['migration', 'mig-1']);
      vi.advanceTimersByTime(399);
      expect(spy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(spy.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([['migrations'], ['stats'], ['migration', 'mig-1']]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('starts a new batch after a flush, so a later event is not lost', () => {
    vi.useFakeTimers();
    try {
      const { spy, batch } = batched(100);
      batch.invalidate(['stats']);
      vi.advanceTimersByTime(100);
      batch.invalidate(['stats']);
      vi.advanceTimersByTime(100);
      expect(spy).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('lets a slow refetch finish while events keep arriving, instead of cancelling it at every flush', async () => {
    vi.useFakeTimers();
    try {
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      let finished = 0;
      const starts: number[] = [];
      // an API that answers in 800 ms, slower than the 400 ms flush
      const observer = new QueryObserver(queryClient, {
        queryKey: ['migrations'],
        queryFn: ({ signal }) =>
          new Promise<number>((resolve, reject) => {
            starts.push(Date.now());
            const timer = setTimeout(() => resolve(++finished), 800);
            signal.addEventListener('abort', () => {
              clearTimeout(timer);
              reject(new DOMException('aborted', 'AbortError'));
            });
          }),
      });
      const unsubscribe = observer.subscribe(() => {});
      await vi.advanceTimersByTimeAsync(800);
      expect(finished).toBe(1);
      const batch = createBatchedInvalidator(queryClient, 400);
      // an event every 300 ms for 6 s
      for (let i = 0; i < 20; i++) {
        batch.invalidate(['migrations']);
        await vi.advanceTimersByTimeAsync(300);
      }
      // the list still refreshed while the events went on
      expect(finished).toBeGreaterThanOrEqual(4);
      // and once they stop, a fetch that began after the last event shows its change
      const lastEvent = Date.now() - 300;
      await vi.advanceTimersByTimeAsync(3000);
      expect(Math.max(...starts)).toBeGreaterThanOrEqual(lastEvent);
      expect(queryClient.isFetching({ queryKey: ['migrations'] })).toBe(0);
      unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('drops pending work on dispose (sign-out, unmount)', () => {
    vi.useFakeTimers();
    try {
      const { spy, batch } = batched();
      batch.invalidate(['migrations']);
      batch.dispose();
      vi.advanceTimersByTime(1000);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});


describe('refetchAfterReconnect', () => {
  it('refetches what the dashboard shows when the stream opens again, not on the first open (SDD §16)', () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue();
    const onStatus = refetchAfterReconnect(queryClient);

    for (const status of ['connecting', 'open'] as const) onStatus(status);
    expect(invalidate).not.toHaveBeenCalled();
    // a drop: the gap may hold persisted events a live-only resume never replays
    for (const status of ['reconnecting', 'reconnecting', 'open'] as const) onStatus(status);
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith();
    onStatus('reconnecting');
    onStatus('open');
    expect(invalidate).toHaveBeenCalledTimes(2);
  });
});

describe('one migration spliced into the cached lists (SDD §16)', () => {
  const other = fx.migrations.find((m) => m.id !== migration.id && m.plan_id === migration.plan_id) as Migration;

  it('asks for the one migration instead of every list, for an event about an existing migration', () => {
    const { queryClient, invalidate } = setup();
    const refresh = vi.fn();
    for (const kind of ['migration.phase', 'migration.error', 'migration.sync_pass', 'advisor.classification', 'decision.approved']) {
      invalidate.mockClear();
      refresh.mockClear();
      applyEventToCache(queryClient, { ...progressEvent({}), seq: 60, kind }, invalidate, refresh);
      expect(refresh, kind).toHaveBeenCalledWith(migration.id);
      expect(invalidate, kind).not.toHaveBeenCalledWith(['migrations']);
    }
    // a new migration is in no cached list yet: the lists are read again
    invalidate.mockClear();
    refresh.mockClear();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 61, kind: 'migration.created' }, invalidate, refresh);
    expect(refresh).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith(['migrations']);
    // a decision that names no migration
    invalidate.mockClear();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 62, kind: 'decision.approved', migration_id: null }, invalidate, refresh);
    expect(invalidate).toHaveBeenCalledWith(['migrations']);
  });

  async function flushed(fetchMigration: (id: string) => Promise<Migration>, lists: Array<[object, Migration[]]>) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    for (const [query, list] of lists) {
      queryClient.setQueryData(['migrations', query], list);
      heldByPage(queryClient, ['migrations', query]);
    }
    const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
    const batch = createBatchedInvalidator(queryClient, 400, fetchMigration);
    batch.refreshMigration(migration.id);
    batch.refreshMigration(migration.id);
    await vi.advanceTimersByTimeAsync(400);
    // what the read queued for the next batch
    await vi.advanceTimersByTimeAsync(400);
    const invalidated = spy.mock.calls.map(([filters]) => filters?.queryKey);
    batch.dispose();
    return { queryClient, invalidated };
  }

  it('reads the migration once per batch and puts it into the lists that hold it, reading the others again', async () => {
    vi.useFakeTimers();
    try {
      const fresh: Migration = { ...migration, phase: 'syncing', progress_pct: 3 };
      const fetchMigration = vi.fn(async () => fresh);
      const { queryClient, invalidated } = await flushed(fetchMigration, [
        [{}, [other, migration]],
        [{ plan_id: migration.plan_id }, [migration]],
        [{ phase: 'precopy' }, [migration]],
        [{ plan_id: 'plan-00000000' }, []],
        [{ plan_id: migration.plan_id, limit: 1 }, [other]],
      ]);
      expect(fetchMigration).toHaveBeenCalledTimes(1);
      expect(fetchMigration).toHaveBeenCalledWith(migration.id);
      expect(queryClient.getQueryData<Migration[]>(['migrations', {}])).toEqual([other, fresh]);
      expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: migration.plan_id }])).toEqual([fresh]);
      expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toEqual(fresh);
      // a list filtered by phase may lose or gain it, a paged one may shift: read again
      expect(invalidated).toContainEqual(['migrations', { phase: 'precopy' }]);
      expect(invalidated).toContainEqual(['migrations', { plan_id: migration.plan_id, limit: 1 }]);
      // another plan's list neither holds it nor could
      expect(invalidated).not.toContainEqual(['migrations', { plan_id: 'plan-00000000' }]);
      expect(invalidated).not.toContainEqual(['migrations', {}]);
      expect(invalidated).not.toContainEqual(['migrations']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('puts the summary of a migration read alone into a summary list (SDD §16, perf audit PF-02)', async () => {
    vi.useFakeTimers();
    try {
      const fresh: Migration = { ...migration, phase: 'syncing', progress_pct: 3 };
      const fetchMigration = vi.fn(async () => fresh);
      const summaries = (list: Migration[]) => list.map(summaryOf) as unknown as Migration[];
      const { queryClient, invalidated } = await flushed(fetchMigration, [
        [{ view: 'summary' }, summaries([other, migration])],
        [{ plan_id: migration.plan_id, view: 'summary' }, summaries([migration])],
        [{ plan_id: migration.plan_id }, [migration]],
      ]);
      expect(fetchMigration).toHaveBeenCalledTimes(1);
      expect(queryClient.getQueryData(['migrations', { view: 'summary' }])).toEqual([summaryOf(other), summaryOf(fresh)]);
      expect(queryClient.getQueryData(['migrations', { plan_id: migration.plan_id, view: 'summary' }])).toEqual([summaryOf(fresh)]);
      expect(queryClient.getQueryData(['migrations', { plan_id: migration.plan_id }])).toEqual([fresh]);
      expect(invalidated).not.toContainEqual(['migrations', { view: 'summary' }]);
      expect(invalidated).not.toContainEqual(['migrations', { plan_id: migration.plan_id, view: 'summary' }]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads nothing for a migration when neither its page nor any migration list is cached (SDD §16, perf PF-04)', async () => {
    vi.useFakeTimers();
    try {
      const fetchMigration = vi.fn(async () => migration);
      // a page that shows no migration (Providers, Inventory): nothing would take the read
      const none = await flushed(fetchMigration, []);
      expect(fetchMigration).not.toHaveBeenCalled();
      expect(none.invalidated).toEqual([]);
      expect(none.queryClient.getQueryData(['migration', migration.id])).toBeUndefined();
      // its own page cached, no list: read once, into the page
      const page = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      page.setQueryData(['migration', migration.id], migration);
      heldByPage(page, ['migration', migration.id]);
      const fresh: Migration = { ...migration, progress_pct: 7 };
      const read = vi.fn(async () => fresh);
      const batch = createBatchedInvalidator(page, 400, read);
      batch.refreshMigration(migration.id);
      await vi.advanceTimersByTimeAsync(400);
      expect(read).toHaveBeenCalledTimes(1);
      expect(page.getQueryData(['migration', migration.id])).toEqual(fresh);
      batch.dispose();
      // any migration list cached: read, and each list decides as before
      await flushed(fetchMigration, [[{ plan_id: 'plan-00000000' }, []]]);
      expect(fetchMigration).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads nothing for a migration no page shows: a disabled list or one kept after its page closed is not shown (SDD §16, audit AUD-1)", async () => {
    vi.useFakeTimers();
    try {
      const fetchMigration = vi.fn(async () => migration);
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      // the plan form's summary list while the form is closed, and a panel's while its plan cannot be edited
      const closed = new QueryObserver(queryClient, { queryKey: ['migrations', { view: 'summary' }], queryFn: async () => [], enabled: false });
      const unsubscribe = closed.subscribe(() => {});
      const panel = new QueryObserver(queryClient, { queryKey: ['migrations', { plan_id: migration.plan_id, view: 'summary' }], queryFn: async () => [], enabled: false });
      const unsubscribePanel = panel.subscribe(() => {});
      // a list and a migration page kept in the cache after their page closed
      queryClient.setQueryData(['migrations', { plan_id: migration.plan_id }], [migration]);
      queryClient.setQueryData(['migration', migration.id], migration);
      const batch = createBatchedInvalidator(queryClient, 400, fetchMigration);
      for (let i = 0; i < 5; i++) batch.refreshMigration(migration.id);
      await vi.advanceTimersByTimeAsync(800);
      expect(fetchMigration).not.toHaveBeenCalled();
      // once a page shows one of them, the event reads the migration again
      const stop = heldByPage(queryClient, ['migrations', { plan_id: migration.plan_id }]);
      batch.refreshMigration(migration.id);
      await vi.advanceTimersByTimeAsync(400);
      expect(fetchMigration).toHaveBeenCalledTimes(1);
      stop();
      unsubscribe();
      unsubscribePanel();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('marks what it does not read stale, so a page opened after the event - Back included - reads it again (SDD §16, review RR-05)', async () => {
    vi.useFakeTimers();
    try {
      const fresh: Migration = { ...migration, phase: 'syncing' };
      const fetchMigration = vi.fn(async () => fresh);
      // the dashboard's default: a read stays fresh for 5 s (main.tsx)
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5_000 } } });
      const listKey = ['migrations', { plan_id: migration.plan_id }];
      // a migration page and a list kept in the cache after their page closed, read just before the event
      queryClient.setQueryData(['migration', migration.id], migration);
      queryClient.setQueryData(['migration', other.id], other);
      queryClient.setQueryData(listKey, [migration, other]);
      const batch = createBatchedInvalidator(queryClient, 400, fetchMigration);
      batch.refreshMigration(migration.id);
      await vi.advanceTimersByTimeAsync(400);
      expect(fetchMigration).not.toHaveBeenCalled();
      // another migration's page is not marked: no event named it
      expect(queryClient.getQueryState(['migration', other.id])?.isInvalidated).toBe(false);
      // Back to the migration's page and to the list, within the 5 s: each reads again as it opens
      const readPage = vi.fn(async () => fresh);
      const stopPage = new QueryObserver(queryClient, { queryKey: ['migration', migration.id], queryFn: readPage }).subscribe(() => {});
      const readList = vi.fn(async () => [fresh, other]);
      const stopList = new QueryObserver(queryClient, { queryKey: listKey, queryFn: readList }).subscribe(() => {});
      await vi.advanceTimersByTimeAsync(0);
      expect(readPage).toHaveBeenCalledTimes(1);
      expect(readList).toHaveBeenCalledTimes(1);
      expect(queryClient.getQueryData(['migration', migration.id])).toEqual(fresh);
      stopPage();
      stopList();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads a list again that should hold the migration but does not, and every list when the read fails', async () => {
    vi.useFakeTimers();
    try {
      const missing = await flushed(async () => migration, [[{ plan_id: migration.plan_id }, [other]]]);
      expect(missing.invalidated).toContainEqual(['migrations', { plan_id: migration.plan_id }]);

      const failed = await flushed(async () => {
        throw new Error('502');
      }, [[{}, [migration]]]);
      expect(failed.invalidated).toContainEqual(['migrations']);
      expect(failed.queryClient.getQueryData<Migration[]>(['migrations', {}])).toEqual([migration]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('refreshes nothing on a saved lesson: it changes no migration, list or figure (SDD §16)', () => {
    const { queryClient, invalidate } = setup();
    const refresh = vi.fn();
    applyEventToCache(queryClient, { ...progressEvent({ type: 'fact' }), seq: 63, kind: 'memory.lesson_saved' }, invalidate, refresh);
    expect(invalidate).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  /** An observed migration list whose every read takes `ms`, counting its reads. */
  function slowList(client: QueryClient, key: unknown[], ms: number) {
    let reads = 0;
    const observer = new QueryObserver(client, {
      queryKey: key,
      queryFn: () => {
        reads += 1;
        return new Promise<Migration[]>((resolve) => setTimeout(() => resolve([migration]), ms));
      },
    });
    const unsubscribe = observer.subscribe(() => {});
    return { reads: () => reads, observer, unsubscribe };
  }

  it('reads a list once when the same batch refreshes it and splices a migration into it (SDD §16)', async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const list = slowList(client, ['migrations', { plan_id: migration.plan_id }], 300);
      await vi.advanceTimersByTimeAsync(300);
      expect(list.reads()).toBe(1);
      const fresh: Migration = { ...migration, phase: 'completed' };
      const batch = createBatchedInvalidator(client, 400, (id) => new Promise((resolve) => setTimeout(() => resolve({ ...fresh, id }), 100)));
      // a plan event refreshes every list; the migration's own event splices it in, in the same batch
      applyEventToCache(client, { ...progressEvent({}), seq: 80, kind: 'plan.updated', migration_id: null }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 81, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      for (let i = 0; i < 6; i += 1) await vi.advanceTimersByTimeAsync(500);
      // the batch's own read started after both events: no second read of a megabyte list
      expect(list.reads()).toBe(2);
      list.unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  /** An observed migration list whose reads answer in turn from `answers`: a list, or an error after `ms`. */
  function scriptedList(client: QueryClient, key: unknown[], answers: Array<{ ms: number; fail?: boolean }>) {
    let reads = 0;
    const observer = new QueryObserver(client, {
      queryKey: key,
      queryFn: () => {
        const answer = answers[Math.min(reads, answers.length - 1)]!;
        reads += 1;
        return new Promise<Migration[]>((resolve, reject) =>
          setTimeout(() => (answer.fail ? reject(new Error('503')) : resolve([migration])), answer.ms),
        );
      },
    });
    const unsubscribe = observer.subscribe(() => {});
    return { reads: () => reads, unsubscribe };
  }

  it("reads a list once more when the batch's own read of it fails while a splice waits on it (SDD §16)", async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const key = ['migrations', { plan_id: migration.plan_id }];
      const list = scriptedList(client, key, [{ ms: 0 }, { ms: 300, fail: true }, { ms: 0 }]);
      await vi.advanceTimersByTimeAsync(10);
      const batch = createBatchedInvalidator(client, 400, async (id) => ({ ...migration, id, phase: 'completed' }));
      applyEventToCache(client, { ...progressEvent({}), seq: 83, kind: 'plan.updated', migration_id: null }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 84, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      for (let i = 0; i < 6; i += 1) await vi.advanceTimersByTimeAsync(500);
      // the failed read left the splice's migration out: the list is read again, and only once
      expect(list.reads()).toBe(3);
      list.unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("puts the migration into a list whose read by the same batch failed before the splice, as before (SDD §16)", async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const key = ['migrations', { plan_id: migration.plan_id }];
      const list = scriptedList(client, key, [{ ms: 0 }, { ms: 50, fail: true }]);
      await vi.advanceTimersByTimeAsync(10);
      const fresh: Migration = { ...migration, phase: 'completed' };
      const other: Migration = { ...migration, id: 'mig-other', phase: 'completed' };
      client.setQueryData(key, [migration, { ...other, phase: 'cutover' }]);
      const read = (id: string) => new Promise<Migration>((resolve) => setTimeout(() => resolve(id === other.id ? other : fresh), 100));
      const batch = createBatchedInvalidator(client, 400, read);
      applyEventToCache(client, { ...progressEvent({}), seq: 85, kind: 'plan.updated', migration_id: null }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 86, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 87, kind: 'migration.phase', migration_id: other.id }, batch.invalidate, batch.refreshMigration);
      await vi.advanceTimersByTimeAsync(600);
      // both splices land, the second after the first wrote into the list
      expect(client.getQueryData<Migration[]>(key)?.map((m) => m.phase)).toEqual(['completed', 'completed']);
      expect(list.reads()).toBe(2);
      list.unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('puts every migration of the batch into a cached list no page shows, which the batch only marks stale (SDD §16)', async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const key = ['migrations', { plan_id: migration.plan_id }];
      const other: Migration = { ...migration, id: 'mig-other', phase: 'cutover' };
      client.setQueryData(key, [migration, other]);
      // a page shows another list, so the reads happen (SDD §16)
      client.setQueryData(['migrations', {}], [migration, other]);
      heldByPage(client, ['migrations', {}]);
      const batch = createBatchedInvalidator(client, 400, async (id) => ({ ...(id === other.id ? other : migration), phase: 'completed' }));
      applyEventToCache(client, { ...progressEvent({}), seq: 88, kind: 'plan.updated', migration_id: null }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 89, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      applyEventToCache(client, { ...progressEvent({}), seq: 90, kind: 'migration.phase', migration_id: other.id }, batch.invalidate, batch.refreshMigration);
      await vi.advanceTimersByTimeAsync(500);
      expect(client.getQueryData<Migration[]>(key)?.map((m) => m.phase)).toEqual(['completed', 'completed']);
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads a list again whose read was under way before the batch, so the event still shows (SDD §16)', async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const list = slowList(client, ['migrations', { plan_id: migration.plan_id }], 1000);
      await vi.advanceTimersByTimeAsync(1000);
      void list.observer.refetch();
      expect(list.reads()).toBe(2);
      const batch = createBatchedInvalidator(client, 400, async () => ({ ...migration, phase: 'completed' }));
      applyEventToCache(client, { ...progressEvent({}), seq: 82, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      for (let i = 0; i < 8; i += 1) await vi.advanceTimersByTimeAsync(500);
      expect(list.reads()).toBe(3);
      list.unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads a list again that has no data yet - its load failed or is under way - as before the splice', async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const key = ['migrations', { plan_id: migration.plan_id }];
      let calls = 0;
      const observer = new QueryObserver(client, {
        queryKey: key,
        queryFn: () => {
          calls += 1;
          return calls === 1 ? Promise.reject(new Error('503')) : Promise.resolve([migration]);
        },
      });
      const unsubscribe = observer.subscribe(() => {});
      await vi.waitFor(() => expect(client.getQueryState(key)?.status).toBe('error'));
      const batch = createBatchedInvalidator(client, 400, async () => migration);
      applyEventToCache(client, { ...progressEvent({}), seq: 70, kind: 'migration.phase' }, batch.invalidate, batch.refreshMigration);
      for (let i = 0; i < 5; i += 1) await vi.advanceTimersByTimeAsync(500);
      expect(calls).toBe(2);
      unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it("never turns a loaded migration's page into a failure on one failed single read; its own query retries", async () => {
    vi.useFakeTimers();
    try {
      const client = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000 } } });
      const key = ['migration', migration.id];
      let calls = 0;
      const get = async (): Promise<Migration> => {
        calls += 1;
        if (calls === 2) throw new ApiError(502, 'provider_error', 'a router blip');
        return { ...migration };
      };
      // useMigration's retry: a 5xx twice
      const observer = new QueryObserver(client, { queryKey: key, queryFn: get, retry: (count: number, error: Error) => !(error instanceof ApiError && error.status < 500) && count < 2 });
      const unsubscribe = observer.subscribe(() => {});
      await vi.waitFor(() => expect(client.getQueryState(key)?.status).toBe('success'));
      const batch = createBatchedInvalidator(client, 400, get);
      applyEventToCache(client, { ...progressEvent({}), seq: 71, kind: 'migration.sync_pass' }, batch.invalidate, batch.refreshMigration);
      for (let i = 0; i < 20; i += 1) await vi.advanceTimersByTimeAsync(500);
      expect(client.getQueryState(key)?.status).toBe('success');
      unsubscribe();
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the browser's page visibility (SDD §16, perf audit PF-03)", () => {
  it("is hidden only for document.visibilityState 'hidden', and hears the page shown again on visibilitychange", () => {
    const state = vi.spyOn(Document.prototype, 'visibilityState', 'get');
    try {
      state.mockReturnValue('visible');
      expect(documentVisibility.hidden()).toBe(false);
      state.mockReturnValue('hidden');
      expect(documentVisibility.hidden()).toBe(true);
      const shown = vi.fn();
      const stop = documentVisibility.onVisible(shown);
      document.dispatchEvent(new Event('visibilitychange'));
      expect(shown).not.toHaveBeenCalled(); // still hidden
      state.mockReturnValue('visible');
      document.dispatchEvent(new Event('visibilitychange'));
      expect(shown).toHaveBeenCalledTimes(1);
      stop();
      document.dispatchEvent(new Event('visibilitychange'));
      expect(shown).toHaveBeenCalledTimes(1);
    } finally {
      state.mockRestore();
    }
    // jsdom, like a page in front, is not hidden: the batch flushes as before
    expect(document.visibilityState).not.toBe('hidden');
    expect(documentVisibility.hidden()).toBe(false);
  });
});

describe('a hidden page holds the live-event batch (SDD §16, perf audit PF-03)', () => {
  it('reads nothing while the page is hidden and refreshes once, as one batch, when it is shown again', async () => {
    vi.useFakeTimers();
    try {
      const fx2 = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
      const one = fx2.migrations.find((m) => m.phase === 'precopy') as Migration;
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      queryClient.setQueryData(['migrations', {}], [one]);
      heldByPage(queryClient, ['migrations', {}]);
      const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
      const fetchMigration = vi.fn(async () => one);
      let hidden = true;
      let shown: (() => void) | null = null;
      const stopWatching = vi.fn();
      const visibility = {
        hidden: () => hidden,
        onVisible: (listener: () => void) => {
          shown = listener;
          return stopWatching;
        },
      };
      const batch = createBatchedInvalidator(queryClient, 400, fetchMigration, visibility);
      batch.invalidate(['plans']);
      batch.invalidate(['stats']);
      batch.invalidate(['plans']);
      batch.refreshMigration(one.id);
      // a background tab: no batch timer wakes it - counted on the fake clock: a spy on the global setTimeout, made
      // while the fake timers are installed, would be restored to the fake before the next test (restoreMocks), and no
      // later test's own timer would ever fire
      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(5000);
      // nothing is read again
      expect(spy).not.toHaveBeenCalled();
      expect(fetchMigration).not.toHaveBeenCalled();
      hidden = false;
      shown!();
      await vi.advanceTimersByTimeAsync(400);
      expect(spy.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([['plans'], ['stats']]);
      expect(fetchMigration).toHaveBeenCalledTimes(1);
      // shown with nothing waiting: nothing more is read
      shown!();
      await vi.advanceTimersByTimeAsync(400);
      expect(spy).toHaveBeenCalledTimes(2);
      batch.dispose();
      expect(stopWatching).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads a migration once the page is shown again, also when nothing else waits', async () => {
    vi.useFakeTimers();
    try {
      const queryClient = new QueryClient();
      const page = { ...(buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0] as Migration), id: 'mig-0000000001' };
      // its page shows it, so the read is wanted (PF-04)
      queryClient.setQueryData(['migration', page.id], page);
      heldByPage(queryClient, ['migration', page.id]);
      const fetchMigration = vi.fn(async () => page);
      let hidden = true;
      let shown: (() => void) | null = null;
      const batch = createBatchedInvalidator(queryClient, 400, fetchMigration, {
        hidden: () => hidden,
        onVisible: (listener) => {
          shown = listener;
          return () => {};
        },
      });
      batch.refreshMigration('mig-0000000001');
      await vi.advanceTimersByTimeAsync(2000);
      expect(fetchMigration).not.toHaveBeenCalled();
      hidden = false;
      shown!();
      await vi.advanceTimersByTimeAsync(400);
      expect(fetchMigration).toHaveBeenCalledWith('mig-0000000001');
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('holds a batch whose timer started before the page was hidden', async () => {
    vi.useFakeTimers();
    try {
      const queryClient = new QueryClient();
      const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
      let hidden = false;
      let shown: (() => void) | null = null;
      const batch = createBatchedInvalidator(queryClient, 400, undefined, {
        hidden: () => hidden,
        onVisible: (listener) => {
          shown = listener;
          return () => {};
        },
      });
      batch.invalidate(['plans']);
      hidden = true;
      await vi.advanceTimersByTimeAsync(2000);
      expect(spy).not.toHaveBeenCalled();
      hidden = false;
      shown!();
      await vi.advanceTimersByTimeAsync(400);
      expect(spy).toHaveBeenCalledTimes(1);
      batch.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('a batch with many migrations to read (SDD §16, perf audit PF-03)', () => {
  async function batchOf(count: number) {
    const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0] as Migration;
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // a list a page shows, so the reads are wanted (PF-04)
    queryClient.setQueryData(['migrations', {}], [base]);
    heldByPage(queryClient, ['migrations', {}]);
    const spy = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined);
    const fetchMigration = vi.fn(async (id: string) => ({ ...base, id }));
    const batch = createBatchedInvalidator(queryClient, 400, fetchMigration);
    for (let n = 0; n < count; n++) batch.refreshMigration(`mig-${String(n).padStart(10, '0')}`);
    await vi.advanceTimersByTimeAsync(400);
    batch.dispose();
    return { fetchMigration, invalidated: spy.mock.calls.map(([filters]) => filters?.queryKey) };
  }

  it('reads up to 50 migrations one by one, and past 50 reads the lists and pages again instead', async () => {
    vi.useFakeTimers();
    try {
      const fifty = await batchOf(50);
      expect(fifty.fetchMigration).toHaveBeenCalledTimes(50);
      expect(fifty.invalidated).not.toContainEqual(['migrations']);
      const many = await batchOf(51);
      expect(many.fetchMigration).not.toHaveBeenCalled();
      expect(many.invalidated).toEqual(expect.arrayContaining([['migrations'], ['migration']]));
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the window regaining focus and the open stream (SDD §16, AUD-3)', () => {
  /** A query a page shows, read once; how often its query function ran, and how to stop showing it. */
  async function shown(queryClient: QueryClient, queryKey: QueryKey, fail = false) {
    let reads = 0;
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn: async () => {
        reads += 1;
        if (fail) throw new Error('down');
        return [];
      },
    });
    const stop = observer.subscribe(() => {});
    await vi.waitFor(() => expect(reads).toBe(1));
    return { reads: () => reads, stop };
  }

  /** The window loses and regains focus once the queries are stale; what focus reads has started. */
  async function refocus() {
    await new Promise((resolve) => setTimeout(resolve, 20));
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await new Promise((resolve) => setTimeout(resolve, 20));
  }

  async function withClient(test: (queryClient: QueryClient, status: { now: LiveStatus }) => Promise<void>) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 10, refetchOnWindowFocus: true, retry: false } } });
    queryClient.mount();
    const status = { now: 'open' as LiveStatus };
    const stop = keepStreamFresh(queryClient, () => status.now);
    try {
      await test(queryClient, status);
    } finally {
      stop();
      queryClient.unmount();
      focusManager.setFocused(undefined);
    }
  }

  it('reads nothing the open stream keeps, and what it does not keep as before', async () => {
    await withClient(async (queryClient) => {
      const kept = await Promise.all(
        [
          ['migrations', { plan_id: 'p1', view: 'summary' }],
          ['migration', 'm1'],
          ['plans'],
          ['plans', 'p1'],
          ['stats', 'p1'],
          ['providers'],
          ['advisor', 'recommendations', {}],
          ['advisor', 'notes', 20],
          ['advisor', 'policies'],
          ['events', 'tail', {}],
        ].map((key) => shown(queryClient, key)),
      );
      const others = await Promise.all(
        [['me'], ['health'], ['inventory', 'src-1'], ['advisor', 'status'], ['advisor', 'data-sent']].map((key) => shown(queryClient, key)),
      );
      await refocus();
      expect(kept.map((q) => q.reads())).toEqual(kept.map(() => 1));
      await vi.waitFor(() => expect(others.map((q) => q.reads())).toEqual(others.map(() => 2)));
      for (const q of [...kept, ...others]) q.stop();
    });
  });

  it('reads them all on focus while the stream is not open', async () => {
    await withClient(async (queryClient, status) => {
      const list = await shown(queryClient, ['migrations', {}]);
      for (const now of ['connecting', 'reconnecting', 'closed', 'unauthorized', 'idle'] as LiveStatus[]) {
        status.now = now;
        const before = list.reads();
        await refocus();
        await vi.waitFor(() => expect(list.reads()).toBe(before + 1));
      }
      list.stop();
    });
  });

  it('reads one whose last read failed on focus, the stream open or not', async () => {
    await withClient(async (queryClient) => {
      const failed = await shown(queryClient, ['stats', 'p1'], true);
      await refocus();
      await vi.waitFor(() => expect(failed.reads()).toBe(2));
      failed.stop();
    });
  });

  it('once stopped (signed out), focus reads them as before', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 10, refetchOnWindowFocus: true, retry: false } } });
    queryClient.mount();
    try {
      keepStreamFresh(queryClient, () => 'open')();
      const list = await shown(queryClient, ['migrations', {}]);
      await refocus();
      await vi.waitFor(() => expect(list.reads()).toBe(2));
      list.stop();
    } finally {
      queryClient.unmount();
      focusManager.setFocused(undefined);
    }
  });
});
