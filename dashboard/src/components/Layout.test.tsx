import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import { Layout } from './Layout';

describe('Layout health notice', () => {
  it('stays silent while the control plane is healthy', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    expect(screen.queryByText(/control plane degraded/i)).not.toBeInTheDocument();
  });

  it('shows the orchestrator status in the session footer', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    const status = await screen.findByTestId('orchestrator-status');
    expect(status).toHaveTextContent(/orchestrator running · last tick 0 s ago/i);
  });

  it('names the degraded parts: database and a stalled orchestrator', async () => {
    const server = createTestServer();
    server.health = {
      ...server.health,
      status: 'degraded',
      db: 'error',
      orchestrator: { running: true, last_tick_age_s: 42.4, ticks: 10, healthy: false },
    };
    renderWithApp(<Layout />, { route: '/', path: '/', server });
    expect(await screen.findByTestId('orchestrator-status')).toHaveTextContent(/stalled/i);
    const notice = await screen.findByRole('status', { name: '' });
    expect(notice).toHaveTextContent(/control plane degraded: the database is unreachable; the orchestrator has not ticked for 42 s/i);
  });

  it('reports a dead orchestrator loop', async () => {
    const server = createTestServer();
    server.health = {
      ...server.health,
      status: 'degraded',
      orchestrator: { running: false, last_tick_age_s: null, ticks: 0, healthy: false },
    };
    renderWithApp(<Layout />, { route: '/', path: '/', server });
    expect(await screen.findByText(/the orchestrator loop is not running/i)).toBeInTheDocument();
  });
});
