import { QueryClient, QueryClientProvider, type QueryKey } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { ApiProvider } from './ApiProvider';
import { ApiClient } from './client';
import { fetchEventTail, queryKeys, useCheckAllProviders, useDeleteProvider, useMigrationAction, usePlanAction, useSetStrategy } from './hooks';
import { createMockFetch, MockServer } from './mock';

function setup() {
  const server = new MockServer({ now: () => Date.parse('2026-10-08T12:00:00Z'), seed: 1 });
  const mockFetch = createMockFetch(server);
  const urls: string[] = [];
  const client = new ApiClient({
    getToken: () => 'viewer',
    fetchImpl: (input, init) => {
      urls.push(String(input));
      return mockFetch(input, init);
    },
  });
  return { server, client, urls };
}

describe('fetchEventTail', () => {
  it('reads the newest events with one tail request, however many are persisted (SDD §12)', async () => {
    const { server, client, urls } = setup();
    for (let i = 0; i < 2500; i += 1) {
      server.emit({ kind: 'plan.updated', plan_id: null, migration_id: null, actor: 'test', message: `n${i}`, data: {} });
    }

    const tail = await fetchEventTail(client, {}, 500);
    expect(urls).toHaveLength(1);
    const query = new URL(urls[0] ?? '', 'http://dashboard.test').searchParams;
    expect([query.get('tail'), query.get('limit'), query.get('since')]).toEqual(['true', '500', null]);
    expect(tail.map((e) => e.seq)).toEqual(server.events.slice(-500).map((e) => e.seq));
  });

  it("keeps a migration timeline's filter (SDD §12)", async () => {
    const { server, client, urls } = setup();
    const target = server.migrations[0];
    if (!target) throw new Error('no migration fixture');
    for (let i = 0; i < 400; i += 1) {
      server.emit({ kind: 'plan.updated', plan_id: target.plan_id, migration_id: target.id, actor: 'test', message: `n${i}`, data: {} });
    }

    const tail = await fetchEventTail(client, { migration_id: target.id }, 300);
    expect(urls).toHaveLength(1);
    expect(new URL(urls[0] ?? '', 'http://dashboard.test').searchParams.get('migration_id')).toBe(target.id);
    const mine = server.events.filter((e) => e.migration_id === target.id);
    expect(tail.map((e) => e.seq)).toEqual(mine.slice(-300).map((e) => e.seq));
  });
});

/** A hook's wrapper over the mock API as `token`, with a query cache to seed and inspect. */
function hookSetup(token: string) {
  const server = new MockServer({ now: () => Date.parse('2026-10-08T12:00:00Z'), seed: 1 });
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={client}>{children}</ApiProvider>
    </QueryClientProvider>
  );
  const invalidated = (queryKey: QueryKey) => queryClient.getQueryState(queryKey)?.isInvalidated ?? false;
  return { server, client, queryClient, wrapper, invalidated };
}

describe('usePlanAction', () => {
  it('refreshes the recommendations after a validation, which records them (SDD §14.4)', async () => {
    const { server, queryClient, wrapper } = hookSetup('operator');
    const feed = queryKeys.recommendations({ limit: 20, offset: 0 });
    queryClient.setQueryData(feed, { items: [], total: 0, offset: 0, limit: 20 });
    const plan = server.plans.find((p) => p.status === 'validated')!;
    const { result } = renderHook(() => usePlanAction(plan.id), { wrapper });
    await result.current.mutateAsync({ action: 'validate' });
    expect(queryClient.getQueryState(feed)?.isInvalidated).toBe(true);
  });
});

describe('mutations refresh what they change (audit C-05)', () => {
  it("useCheckAllProviders refreshes each checked provider's inventory, as one Check does", async () => {
    const { server, queryClient, wrapper, invalidated } = hookSetup('operator');
    const ids = server.providers.map((p) => p.id);
    for (const id of ids) queryClient.setQueryData(queryKeys.inventory(id), []);
    queryClient.setQueryData(queryKeys.providers, []);
    const { result } = renderHook(() => useCheckAllProviders(), { wrapper });
    const outcome = await result.current.mutateAsync(ids);
    expect(outcome.checked.map((p) => p.id)).toEqual(ids);
    expect(invalidated(queryKeys.providers)).toBe(true);
    for (const id of ids) expect(invalidated(queryKeys.inventory(id)), id).toBe(true);
  });

  it("useDeleteProvider drops the deleted provider's cached inventory", async () => {
    const { server, queryClient, wrapper, invalidated } = hookSetup('admin');
    // a provider no plan needs, so the API lets it go (SDD §12)
    server.providers.push({ ...server.providers[0]!, id: 'spare-dc9', name: 'Spare DC9' });
    queryClient.setQueryData(queryKeys.inventory('spare-dc9'), []);
    queryClient.setQueryData(queryKeys.providers, []);
    const { result } = renderHook(() => useDeleteProvider(), { wrapper });
    await result.current.mutateAsync('spare-dc9');
    expect(queryClient.getQueryCache().find({ queryKey: queryKeys.inventory('spare-dc9') })).toBeUndefined();
    expect(invalidated(queryKeys.providers)).toBe(true);
  });

  it('useSetStrategy refreshes the plan, the plan list, the figures and the events: the API records the override in the plan', async () => {
    const { server, queryClient, wrapper, invalidated } = hookSetup('operator');
    const migration = server.migrations.find((m) => ['pending', 'ready', 'blocked'].includes(m.phase) && !m.advisor_hold && m.estimates.some((e) => e.eligible))!;
    const strategy = migration.estimates.find((e) => e.eligible)!.strategy;
    const keys: QueryKey[] = [queryKeys.plan(migration.plan_id), queryKeys.plans, queryKeys.stats(), queryKeys.eventTail()];
    for (const key of keys) queryClient.setQueryData(key, []);
    const { result } = renderHook(() => useSetStrategy(migration.id), { wrapper });
    await result.current.mutateAsync(strategy);
    for (const key of keys) expect(invalidated(key), JSON.stringify(key)).toBe(true);
  });

  it('useMigrationAction refreshes the plan list too: its status and counts follow the action', async () => {
    const { server, queryClient, wrapper, invalidated } = hookSetup('operator');
    const migration = server.migrations.find((m) => m.phase === 'pending')!;
    queryClient.setQueryData(queryKeys.plans, []);
    const { result } = renderHook(() => useMigrationAction(migration.id), { wrapper });
    await result.current.mutateAsync({ action: 'cancel', body: { reason: 'no longer in scope' } });
    expect(invalidated(queryKeys.plans)).toBe(true);
  });
});
