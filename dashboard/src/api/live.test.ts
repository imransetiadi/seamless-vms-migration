import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { applyEventToCache, createBatchedInvalidator, refetchAfterReconnect } from './live';
import { buildFixtures } from './mockData';
import type { Event, Migration } from './types';

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
    expect(invalidate).toHaveBeenCalledWith(['plans', migration.plan_id]);
    expect(invalidate).toHaveBeenCalledWith(['stats']);

    invalidate.mockClear();
    applyEventToCache(queryClient, { ...progressEvent({}), seq: 43, kind: 'provider.checked', migration_id: null, plan_id: null }, invalidate);
    expect(invalidate).toHaveBeenCalledWith(['providers']);
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
