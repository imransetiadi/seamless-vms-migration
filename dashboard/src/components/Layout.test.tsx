import { act, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LiveContext, LiveEventHub } from '../api/live';
import { createTestServer, renderWithApp } from '../test/utils';
import { Layout } from './Layout';

describe('Layout health notice', () => {
  it('stays silent while the control plane is healthy', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    expect(screen.queryByText(/control plane degraded/i)).not.toBeInTheDocument();
  });

  it('shows the brand mark beside the product name, which names the link (SDD §16)', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    const links = screen.getAllByRole('link', { name: 'Seamless Migrate to RHOSO 18.0' });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) expect(link.querySelector('svg[data-brand-mark]')).toHaveAttribute('aria-hidden', 'true');
  });

  it('shows the orchestrator status in the session footer', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    const status = await screen.findByTestId('orchestrator-status');
    expect(status).toHaveTextContent(/orchestrator running · last tick 0 s ago/i);
  });

  it('names the control plane version in the session footer, not only on the sign-in page', async () => {
    const server = createTestServer();
    const version = (server.handle('GET', '/health', new URLSearchParams(), undefined, null).body as { version: string }).version;
    // no token: the anonymous admin of auth-disabled demo mode, who never sees the sign-in page (SDD §13.1)
    renderWithApp(<Layout />, { route: '/', path: '/', server, token: null });
    expect(await screen.findByText(`Control plane ${version}`)).toBeInTheDocument();
    expect(await screen.findByText('anonymous')).toBeInTheDocument();
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
    const notice = await screen.findByTestId('health-notice');
    expect(notice).toHaveTextContent(
      /control plane degraded: the database is unreachable; the orchestrator loop is unhealthy \(last tick 42 s ago\)/i,
    );
  });

  it('keeps the live region mounted while healthy so a later notice is announced', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    const region = screen.getByTestId('health-notice');
    expect(region).toHaveAttribute('role', 'status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toBeEmptyDOMElement();
  });

  it('says once for the page when the live stream drops, comes back or stops, not its first connection (SDD §16)', async () => {
    const hub = new LiveEventHub();
    renderWithApp(
      <LiveContext.Provider value={hub}>
        <Layout />
      </LiveContext.Provider>,
      { route: '/', path: '/' },
    );
    await screen.findByRole('navigation', { name: /primary/i });
    const regions = screen.getAllByRole('status');
    const said = () => regions.map((r) => r.textContent ?? '').filter((t) => /live updates|signed out/i.test(t));
    // the first connection, failed attempts before it included, is not news
    act(() => hub.setStatus('connecting'));
    act(() => hub.setStatus('reconnecting'));
    act(() => hub.setStatus('open'));
    expect(said()).toEqual([]);
    act(() => hub.setStatus('reconnecting'));
    expect(said()).toEqual(['Live updates lost: reconnecting.']);
    act(() => hub.setStatus('open'));
    expect(said()).toEqual(['Live updates back.']);
    act(() => hub.setStatus('closed'));
    expect(said()).toEqual(['Live updates stopped: the page shows what it last received.']);
    act(() => hub.setStatus('unauthorized'));
    expect(said()).toEqual(['Signed out: live updates stopped.']);
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
