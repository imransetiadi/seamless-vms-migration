import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiProvider } from './ApiProvider';
import { ApiClient } from './client';
import { LiveEventsProvider } from './LiveEventsProvider';

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
});
