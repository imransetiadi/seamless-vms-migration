import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { useApiQuery } from './stickyQuery';

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('useApiQuery (SDD §16 Failed loads)', () => {
  it('keeps the last error while the query is fetched again, until an answer replaces it', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let answer: () => Promise<string> = () => Promise.reject(new Error('first failure'));
    let release: () => void = () => {};
    const { result } = renderHook(() => useApiQuery({ queryKey: ['thing'], queryFn: () => answer() }), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.error?.message).toBe('first failure'));

    // a refetch that is still running: TanStack would report pending with no error
    answer = () => new Promise<string>((resolve) => (release = () => resolve('ok')));
    act(() => void result.current.refetch());
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.isPending).toBe(false);
    expect(result.current.status).toBe('error');
    expect(result.current.error?.message).toBe('first failure');

    // the answer replaces it
    act(() => release());
    await waitFor(() => expect(result.current.data).toBe('ok'));
    expect(result.current.error).toBeNull();
  });

  it('never carries an error over to another query key', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let failing = true;
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) =>
        useApiQuery({ queryKey: ['thing', id], queryFn: () => (failing ? Promise.reject(new Error(`no ${id}`)) : new Promise<string>(() => {})) }),
      { wrapper: wrapper(client), initialProps: { id: 'a' } },
    );
    await waitFor(() => expect(result.current.error?.message).toBe('no a'));
    failing = false;
    rerender({ id: 'b' });
    // b is loading for the first time: pending, without a's error
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.isPending).toBe(true);
    expect(result.current.error).toBeNull();
  });
});
