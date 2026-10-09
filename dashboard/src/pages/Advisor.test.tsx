import { screen } from '@testing-library/react';
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
});
