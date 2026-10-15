import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ApiProvider } from '../api/ApiProvider';
import { getStoredToken } from '../api/auth';
import { ApiClient } from '../api/client';
import { queryKeys } from '../api/hooks';
import { LiveEventsProvider } from '../api/LiveEventsProvider';
import { createMockFetch, MockServer } from '../api/mock';
import { ThemeProvider } from '../theme/ThemeProvider';


/** Fixed clock for deterministic fixtures. */
export const TEST_NOW = Date.parse('2026-10-08T12:00:00Z');

/** What scrollIntoView was called on, and how: jsdom has no scrollIntoView of its own (SDD §16). */
export function recordScrolls(): { scrolls: Array<{ element: Element; options: unknown }>; restore: () => void } {
  const proto = Element.prototype as unknown as Record<string, unknown>;
  const own = Object.getOwnPropertyDescriptor(proto, 'scrollIntoView');
  const scrolls: Array<{ element: Element; options: unknown }> = [];
  proto.scrollIntoView = function (this: Element, options?: unknown) {
    scrolls.push({ element: this, options });
  };
  return {
    scrolls,
    restore: () => {
      if (own) Object.defineProperty(proto, 'scrollIntoView', own);
      else delete proto.scrollIntoView;
    },
  };
}

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
  /** Wraps the mock fetch, e.g. to hold one request pending and see a loading state. */
  wrapFetch?: (fetchImpl: typeof fetch) => typeof fetch;
  /** Called on every 401, as the app's client calls its handler (main.tsx). */
  onUnauthorized?: () => void;
}

export function createTestServer(): MockServer {
  return new MockServer({ now: () => TEST_NOW, seed: 7 });
}

export function createTestClient(
  server: MockServer,
  token: string | null = 'admin',
  storedToken = false,
  wrapFetch: (fetchImpl: typeof fetch) => typeof fetch = (fetchImpl) => fetchImpl,
  onUnauthorized?: () => void,
): ApiClient {
  return new ApiClient({ getToken: storedToken ? getStoredToken : () => token, fetchImpl: wrapFetch(createMockFetch(server)), onUnauthorized });
}

export function renderWithApp(ui: ReactElement, options: RenderOptions = {}): RenderResult & { server: MockServer; queryClient: QueryClient } {
  const server = options.server ?? createTestServer();
  const client = createTestClient(
    server,
    options.token === undefined ? 'admin' : options.token,
    options.storedToken,
    options.wrapFetch,
    options.onUnauthorized,
  );
  // a hook that sets its own `retry` (noRetryOnClientErrors: twice on a 5xx) keeps this `retryDelay`, so a test of a
  // failed load waits no real 1 s + 2 s; a hook's `retry` overrides the default, its delay does not
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0, staleTime: Infinity, refetchOnWindowFocus: false }, mutations: { retry: false, retryDelay: 0 } },
  });
  // The app renders pages only after RequireAuth has loaded GET /me, so a page never sees an unknown
  // role. Pages rendered here skip RequireAuth: seed the session like it, or a test that clicks a
  // role-gated action races the /me request (the action stays soft-disabled until it returns).
  if (!options.storedToken) {
    const me = server.handle('GET', '/me', new URLSearchParams(), undefined, options.token === undefined ? 'admin' : options.token);
    if (me.status === 200) queryClient.setQueryData(queryKeys.me, me.body);
  }
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
