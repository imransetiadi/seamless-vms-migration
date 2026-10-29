import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { endSession, getStoredToken } from './api/auth';
import { ApiClient, DEFAULT_API_BASE, setDefaultClient } from './api/client';
import { queryKeys } from './api/hooks';
import { createQueryClient } from './api/queryClient';
// SDD §16 fonts, bundled (Latin, the weights in use): an air-gapped install loads nothing from another host
import '@fontsource/fira-code/latin-400.css';
import '@fontsource/fira-code/latin-500.css';
import '@fontsource/fira-code/latin-600.css';
import '@fontsource/fira-sans/latin-400.css';
import '@fontsource/fira-sans/latin-500.css';
import '@fontsource/fira-sans/latin-600.css';
import './index.css';

async function bootstrap() {
  const queryClient = createQueryClient({ queries: { staleTime: 5_000, refetchOnWindowFocus: true } });

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
      // Drop a rejected token once, marking that the session ended (Sign in says so, SDD §16); RequireAuth then
      // redirects to /login when /me fails.
      if (endSession()) void queryClient.invalidateQueries({ queryKey: queryKeys.me });
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
