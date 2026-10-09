import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { applyEventToCache, createBatchedInvalidator } from './live';
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

function setup() {
  const queryClient = new QueryClient();
  queryClient.setQueryData(['migration', migration.id], migration);
  queryClient.setQueryData(['migrations', { plan_id: migration.plan_id }], [migration]);
  const invalidate = vi.fn();
  return { queryClient, invalidate };
}

describe('applyEventToCache', () => {
  it('patches progress in place from the mock payload (progress_pct, bytes_transferred)', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, progressEvent({ progress_pct: 55.5, bytes_transferred: 123, bytes_total: 456 }), invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toMatchObject({ progress_pct: 55.5, bytes_transferred: 123, bytes_total: 456 });
    expect(queryClient.getQueryData<Migration[]>(['migrations', { plan_id: migration.plan_id }])?.[0]?.progress_pct).toBe(55.5);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('accepts the executor report_progress shape (pct, bytes_done, bytes_total — SDD §7.1)', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, progressEvent({ pct: 12.5, bytes_done: 1000, bytes_total: 8000 }), invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])).toMatchObject({ progress_pct: 12.5, bytes_transferred: 1000, bytes_total: 8000 });
  });

  it('invalidates instead of guessing when a progress payload has no percentage', () => {
    const { queryClient, invalidate } = setup();
    applyEventToCache(queryClient, progressEvent({ note: 'unknown shape' }), invalidate);
    expect(queryClient.getQueryData<Migration>(['migration', migration.id])?.progress_pct).toBe(migration.progress_pct);
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
