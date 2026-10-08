import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { applyEventToCache } from './live';
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
