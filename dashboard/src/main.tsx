import { QueryClient } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { clearStoredToken, getStoredToken } from './api/auth';
import { ApiClient, DEFAULT_API_BASE, setDefaultClient } from './api/client';
import { queryKeys } from './api/hooks';
import './index.css';

async function bootstrap() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 5_000, refetchOnWindowFocus: true },
    },
  });

  let fetchImpl: typeof fetch | undefined;
  if (import.meta.env.VITE_SEAMLESS_MOCK === '1') {
    // Dead-code-eliminated from normal builds: the mock chunk only exists in mock builds.
    const { MockServer, createMockFetch } = await import('./api/mock');
    const server = new MockServer();
    server.start(1000);
    fetchImpl = createMockFetch(server, { latencyMs: 120 });
    console.info('[seamless] Mock API enabled — tokens: viewer, operator, approver, admin (or none for anonymous admin).');
  }

  const client = new ApiClient({
    baseUrl: import.meta.env.VITE_SEAMLESS_API_BASE || DEFAULT_API_BASE,
    getToken: getStoredToken,
    fetchImpl,
    onUnauthorized: () => {
      // Drop a rejected token once; RequireAuth then redirects to /login when /me fails.
      if (getStoredToken()) {
        clearStoredToken();
        void queryClient.invalidateQueries({ queryKey: queryKeys.me });
      }
    },
  });
  setDefaultClient(client);

  const container = document.getElementById('root');
  if (!container) throw new Error('Missing #root element');
  createRoot(container).render(
    <StrictMode>
      <App client={client} queryClient={queryClient} />
    </StrictMode>,
  );
}

void bootstrap();
