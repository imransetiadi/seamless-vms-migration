import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import Advisor from './Advisor';

/** A mock server whose GET /migrations fails, as a control plane with a broken database would. */
function serverWithoutMigrations() {
  const server = createTestServer();
  const handle = server.handle.bind(server);
  server.handle = (method, path, query, body, token) =>
    method === 'GET' && path === '/migrations'
      ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
      : handle(method, path, query, body, token);
  return server;
}

describe('Advisor page', () => {
  it('says the advisor notes could not be loaded instead of claiming there are none', async () => {
    renderWithApp(<Advisor />, { server: serverWithoutMigrations() });

    // a 5xx is retried twice with backoff before the query reports the error
    expect(await screen.findByText(/advisor notes are unavailable/i, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText('No advisor notes yet.')).not.toBeInTheDocument();
  }, 15_000); // the 5xx retries take about 3 s

  it('says it is still checking agentmemory while the advisor status loads, not that it is off', async () => {
    const pending = new Promise<Response>(() => {});
    renderWithApp(<Advisor />, {
      token: 'operator',
      wrapFetch: (mock) => (input, init) => (String(input).split('?')[0]!.endsWith('/api/v1/advisor/status') ? pending : mock(input, init)),
    });
    const search = await screen.findByRole('button', { name: /^search$/i });
    expect(search).toHaveAccessibleDescription('Checking whether agentmemory is enabled…');
  });

  it('says the advisor status could not be loaded instead of that agentmemory is off (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/advisor/status'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Advisor />, { token: 'operator', server });
    await screen.findByText(/advisor status is unavailable/i);
    expect(screen.getByRole('button', { name: /^search$/i })).toHaveAccessibleDescription('The advisor status could not be loaded.');
  });

  it('says agentmemory is not enabled only when the loaded status says so', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      const result = handle(method, path, query, body, token);
      if (method !== 'GET' || path !== '/advisor/status') return result;
      const status = result.body as { memory: Record<string, unknown> };
      return { ...result, body: { ...status, memory: { ...status.memory, enabled: false } } };
    };
    renderWithApp(<Advisor />, { token: 'operator', server });
    const search = await screen.findByRole('button', { name: /^search$/i });
    await waitFor(() => expect(search).toHaveAccessibleDescription('agentmemory is not enabled.'));
  });

  it('announces the error when the search is submitted empty (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Advisor />, { token: 'operator' });
    const search = await screen.findByRole('button', { name: /^search$/i });
    // the advisor status has loaded: agentmemory is enabled in the mock
    await waitFor(() => expect(search).not.toHaveAttribute('aria-disabled'));
    await user.click(search);
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a few words about the failure to search for.');
  });
});
