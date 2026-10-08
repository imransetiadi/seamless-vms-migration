import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ApiProvider } from '../api/ApiProvider';
import { getStoredToken } from '../api/auth';
import { ApiClient } from '../api/client';
import { LiveEventsProvider } from '../api/LiveEventsProvider';
import { createMockFetch, MockServer } from '../api/mock';
import { ThemeProvider } from '../theme/ThemeProvider';


/** Fixed clock for deterministic fixtures. */
export const TEST_NOW = Date.parse('2026-10-08T12:00:00Z');

export interface RenderOptions {
  /** Mock token selecting the role (`viewer`, `operator`, `approver`, `admin`). */
  token?: string | null;
  route?: string;
  /** Route pattern the element is mounted at (e.g. `/plans/:planId`). */
  path?: string;
  server?: MockServer;
  live?: boolean;
  /** Read the token from sessionStorage like the real app (login flows). */
  storedToken?: boolean;
}

export function createTestServer(): MockServer {
  return new MockServer({ now: () => TEST_NOW, seed: 7 });
}

export function createTestClient(server: MockServer, token: string | null = 'admin', storedToken = false): ApiClient {
  return new ApiClient({ getToken: storedToken ? getStoredToken : () => token, fetchImpl: createMockFetch(server) });
}

export function renderWithApp(ui: ReactElement, options: RenderOptions = {}): RenderResult & { server: MockServer; queryClient: QueryClient } {
  const server = options.server ?? createTestServer();
  const client = createTestClient(server, options.token === undefined ? 'admin' : options.token, options.storedToken);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false }, mutations: { retry: false } },
  });
  const wrap = (node: ReactNode) =>
    options.live ? <LiveEventsProvider enabled>{node}</LiveEventsProvider> : node;
  const result = render(
    <ThemeProvider initialTheme="dark">
      <QueryClientProvider client={queryClient}>
        <ApiProvider client={client}>
          <MemoryRouter initialEntries={[options.route ?? '/']}>
            {wrap(
              <Routes>
                <Route path={options.path ?? '*'} element={ui} />
              </Routes>,
            )}
          </MemoryRouter>
        </ApiProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return Object.assign(result, { server, queryClient });
}
