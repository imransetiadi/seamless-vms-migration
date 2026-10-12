import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { ApiProvider } from './ApiProvider';
import { ApiClient } from './client';
import { fetchEventTail, queryKeys, usePlanAction } from './hooks';
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

describe('usePlanAction', () => {
  it('refreshes the recommendations after a validation, which records them (SDD §14.4)', async () => {
    const server = new MockServer({ now: () => Date.parse('2026-10-08T12:00:00Z'), seed: 1 });
    const client = new ApiClient({ getToken: () => 'operator', fetchImpl: createMockFetch(server) });
    const queryClient = new QueryClient();
    const feed = queryKeys.recommendations({ limit: 20, offset: 0 });
    queryClient.setQueryData(feed, { items: [], total: 0, offset: 0, limit: 20 });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>{children}</ApiProvider>
      </QueryClientProvider>
    );
    const plan = server.plans.find((p) => p.status === 'validated')!;
    const { result } = renderHook(() => usePlanAction(plan.id), { wrapper });
    await result.current.mutateAsync({ action: 'validate' });
    expect(queryClient.getQueryState(feed)?.isInvalidated).toBe(true);
  });
});
