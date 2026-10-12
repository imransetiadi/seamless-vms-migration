import { QueryClient, QueryClientProvider, QueryObserver, type QueryKey } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { summaryOf } from '../lib/summary';
import { ApiProvider } from './ApiProvider';
import { ApiClient } from './client';
import { LiveEventsProvider } from './LiveEventsProvider';
import { buildFixtures } from './mockData';
import type { Migration } from './types';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Hides or shows the page as a browser does: `visibilityState`, then `visibilitychange` (it bubbles to the window). */
function setVisibility(state: 'hidden' | 'visible') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
}

afterEach(() => {
  // back to jsdom's own (the prototype's getter)
  delete (document as { visibilityState?: unknown }).visibilityState;
});

describe('LiveEventsProvider', () => {
  it('refetches the data it shows after the event stream reconnects (SDD §16)', async () => {
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const refetchedAll = () => invalidate.mock.calls.filter((args) => args.length === 0).length;
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      if (!String(input).includes('/events/stream')) return new Response('{}', { status: 200 });
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controllers.push(controller);
        },
      });
      return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    };
    const client = new ApiClient({ getToken: () => 'viewer', fetchImpl });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <LiveEventsProvider enabled>
            <span>dashboard</span>
          </LiveEventsProvider>
        </ApiProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByText('dashboard')).toBeInTheDocument();

    // the first connection opens: nothing to refetch
    await waitFor(() => expect(controllers).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(refetchedAll()).toBe(0);
    // it drops before any persisted event; the reconnect (after the 1 s backoff) refetches once
    controllers[0]?.close();
    await waitFor(() => expect(controllers).toHaveLength(2), { timeout: 4000 });
    await waitFor(() => expect(refetchedAll()).toBe(1));
    unmount();
  });

  it('a page shown again reads what its held events ask for once and nothing the open stream keeps (SDD §16)', async () => {
    // as in main.tsx: a stale query is read again when the window regains focus (a short staleTime for the test)
    const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 50, refetchOnWindowFocus: true, retry: false } } });
    const fx = buildFixtures(Date.parse('2026-10-08T12:00:00Z'));
    const cached = fx.migrations.find((m) => m.phase === 'precopy') as Migration;
    const fresh: Migration = { ...cached, phase: 'syncing' };
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const migrationReads: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      const url = String(input);
      if (url.includes('/events/stream')) {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controllers.push(controller);
          },
        });
        return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
      }
      if (url.includes(`/migrations/${cached.id}`)) {
        migrationReads.push(url);
        return new Response(JSON.stringify(fresh), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response('{}', { status: 200 });
    };
    const client = new ApiClient({ getToken: () => 'viewer', fetchImpl });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <LiveEventsProvider enabled>
            <span>dashboard</span>
          </LiveEventsProvider>
        </ApiProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(controllers).toHaveLength(1));

    // what a plan page shows: its summary list, its figures, the plan list - and the signed-in user, which no event keeps
    const reads = new Map<string, number>();
    const listKey = ['migrations', { plan_id: cached.plan_id, view: 'summary' }] as const;
    const shown: Array<[QueryKey, unknown]> = [
      [listKey, [summaryOf(cached)]],
      [['stats', cached.plan_id], {}],
      [['plans'], []],
      [['me'], {}],
    ];
    const stops = shown.map(([queryKey, data]) =>
      new QueryObserver(queryClient, {
        queryKey,
        queryFn: async () => {
          reads.set(JSON.stringify(queryKey), (reads.get(JSON.stringify(queryKey)) ?? 0) + 1);
          return data;
        },
      }).subscribe(() => {}),
    );
    await waitFor(() => expect([...reads.values()]).toEqual([1, 1, 1, 1]));

    setVisibility('hidden');
    await sleep(100); // all of them are stale now
    const event = { seq: 7, ts: '2026-10-12T10:00:00Z', kind: 'migration.phase', plan_id: cached.plan_id, migration_id: cached.id, actor: 'system', message: 'syncing', data: {} };
    controllers[0]!.enqueue(new TextEncoder().encode(`id: 7\nevent: migration.phase\ndata: ${JSON.stringify(event)}\n\n`));
    await sleep(100); // held while hidden
    expect(migrationReads).toEqual([]);
    expect([...reads.values()]).toEqual([1, 1, 1, 1]);

    setVisibility('visible');
    await sleep(1000); // the batch, and one more after it, have run
    expect(Object.fromEntries(reads)).toEqual({
      [JSON.stringify(listKey)]: 1, // not read again: the migration's read was put into it
      [JSON.stringify(['stats', cached.plan_id])]: 2,
      [JSON.stringify(['plans'])]: 2,
      [JSON.stringify(['me'])]: 2, // what no event keeps is read on focus
    });
    expect(migrationReads).toHaveLength(1);
    expect((queryClient.getQueryData(listKey) as Array<{ phase: string }>)[0]?.phase).toBe('syncing');
    for (const stop of stops) stop();
    unmount();
    // signed out: the queries are read on focus as before
    expect(queryClient.getQueryDefaults(['migrations']).refetchOnWindowFocus).toBeUndefined();
  });

  it('while the stream is not open, the window regaining focus reads what it keeps, as before (SDD §16)', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 50, refetchOnWindowFocus: true, retry: false } } });
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      if (!String(input).includes('/events/stream')) return new Response('{}', { status: 200 });
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controllers.push(controller);
        },
      });
      return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    };
    const client = new ApiClient({ getToken: () => 'viewer', fetchImpl });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <LiveEventsProvider enabled>
            <span>dashboard</span>
          </LiveEventsProvider>
        </ApiProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(controllers).toHaveLength(1));
    let reads = 0;
    const stop = new QueryObserver(queryClient, {
      queryKey: ['migrations', {}],
      queryFn: async () => {
        reads += 1;
        return [];
      },
    }).subscribe(() => {});
    await waitFor(() => expect(reads).toBe(1));
    await sleep(100); // stale
    setVisibility('hidden');
    setVisibility('visible');
    await sleep(100);
    expect(reads).toBe(1); // the open stream keeps it
    // the stream drops: until it is open again (after its 1 s backoff) the window regaining focus reads it
    controllers[0]!.close();
    await sleep(100);
    setVisibility('hidden');
    setVisibility('visible');
    await waitFor(() => expect(reads).toBe(2));
    expect(controllers).toHaveLength(1); // still reconnecting
    stop();
    unmount();
  });
});
