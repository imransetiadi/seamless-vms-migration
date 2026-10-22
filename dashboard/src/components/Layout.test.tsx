import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LiveContext, LiveEventHub } from '../api/live';
import { createTestServer, renderWithApp } from '../test/utils';
import { Layout } from './Layout';

describe('Layout health notice', () => {
  // the session footer with its details is the full sidebar's (SDD §16); the rail shows live, theme and sign out
  beforeEach(() => window.localStorage.setItem('seamless.nav', 'full'));
  afterEach(() => window.localStorage.removeItem('seamless.nav'));

  it('stays silent while the control plane is healthy', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    expect(screen.queryByText(/control plane degraded/i)).not.toBeInTheDocument();
  });

  it('shows the brand mark beside the product name, which names the link (SDD §16)', async () => {
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await screen.findByRole('navigation', { name: /primary/i });
    const links = screen.getAllByRole('link', { name: 'Seamless Migrate to RHOSO 18.0 or OpenStack' });
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
    expect(await within(screen.getByTestId('app-navigation')).findByText('anonymous')).toBeInTheDocument();
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

describe('Layout navigation (SDD §16)', () => {
  beforeEach(() => window.localStorage.removeItem('seamless.nav'));

  it('groups the items, lists the Advisor pages and marks the current one', async () => {
    renderWithApp(<Layout />, { route: '/advisor/recommendations/rec-b2c4d6e87101', path: '/advisor/recommendations/:recId' });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const groups = within(nav).getAllByRole('group');
    expect(groups).toHaveLength(5);
    ['Migrations', 'Clouds', 'Activity', 'Advisor', 'Administration'].forEach((name, i) => expect(groups[i]).toHaveAccessibleName(name));
    const items = (group: HTMLElement) => within(group).getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')]);
    // the hook catalog every role reads, after Plans (SDD §16, plan task N12a)
    expect(items(groups[0]!)).toEqual([['Overview', '/'], ['Plans', '/plans'], ['Hooks', '/hooks']]);
    expect(items(groups[1]!)).toEqual([['Providers', '/providers'], ['Inventory', '/inventory']]);
    expect(items(groups[2]!)).toEqual([['Events', '/events']]);
    expect(items(groups[3]!)).toEqual([['Recommendations', '/advisor'], ['Policies', '/advisor/policies'], ['Audit trail', '/advisor/audit']]);
    expect(items(groups[4]!)).toEqual([['Users', '/users']]);
    // a recommendation's page belongs to Recommendations
    expect(within(nav).getByRole('link', { name: 'Recommendations' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getAllByRole('link').filter((link) => link.getAttribute('aria-current') === 'page')).toHaveLength(1);
  });

  it("marks Plans on a migration's page", async () => {
    renderWithApp(<Layout />, { route: '/migrations/mig-1', path: '/migrations/:migrationId' });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    expect(within(nav).getByRole('link', { name: 'Plans' })).toHaveAttribute('aria-current', 'page');
  });

  it('describes Plans with the failed count from /stats; nothing when none failed or the stats are unknown', async () => {
    const { unmount } = renderWithApp(<Layout />, { route: '/', path: '/' });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    await waitFor(() => expect(within(nav).getByRole('link', { name: 'Plans' })).toHaveAccessibleDescription('1 failed'));
    unmount();

    for (const answer of ['none', 'unknown'] as const) {
      const server = createTestServer();
      const handle = server.handle.bind(server);
      server.handle = (method, path, query, body, token) => {
        if (method !== 'GET' || path !== '/stats') return handle(method, path, query, body, token);
        if (answer === 'unknown') return { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } };
        const result = handle(method, path, query, body, token);
        return { ...result, body: { ...(result.body as Record<string, unknown>), failed: 0 } };
      };
      const view = renderWithApp(<Layout />, { route: '/', path: '/', server });
      const plans = within(await screen.findByRole('navigation', { name: 'Primary' })).getByRole('link', { name: 'Plans' });
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(plans).not.toHaveAccessibleDescription(/failed/);
      view.unmount();
    }
  });

  it('collapses to the rail and expands again, remembered in this browser', async () => {
    const user = userEvent.setup({ delay: null });
    const first = renderWithApp(<Layout />, { route: '/', path: '/' });
    // a narrow window (the test's matchMedia matches nothing) starts as the rail
    const column = await screen.findByTestId('app-navigation');
    expect(column).toHaveAttribute('data-nav', 'rail');
    // the rail keeps every label visible
    expect(within(column).getByRole('link', { name: 'Audit trail' })).toHaveTextContent('Audit trail');
    await user.click(within(column).getByRole('button', { name: 'Expand navigation' }));
    expect(column).toHaveAttribute('data-nav', 'full');
    expect(within(column).getByRole('button', { name: 'Collapse navigation' })).toHaveAttribute('aria-expanded', 'true');
    expect(window.localStorage.getItem('seamless.nav')).toBe('full');
    first.unmount();

    renderWithApp(<Layout />, { route: '/', path: '/' });
    expect(await screen.findByTestId('app-navigation')).toHaveAttribute('data-nav', 'full');
  });

  it('starts as the full sidebar in a wide window without a stored choice', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({ ...original(query), matches: query.includes('1280px') })) as typeof window.matchMedia;
    try {
      renderWithApp(<Layout />, { route: '/', path: '/' });
      expect(await screen.findByTestId('app-navigation')).toHaveAttribute('data-nav', 'full');
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('Layout phone navigation (SDD §16 focus)', () => {
  it('opens a drawer named Navigation with the grouped items and the session', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }));
    const drawer = screen.getByRole('dialog', { name: 'Navigation' });
    const nav = within(drawer).getByRole('navigation', { name: 'Primary' });
    expect(within(nav).getAllByRole('group')).toHaveLength(5);
    expect(within(drawer).getByRole('button', { name: /sign out/i })).toBeInTheDocument();
  });

  it("choosing the page you are on closes the menu and puts the focus on the page, not on nothing", async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }));
    const drawer = screen.getByRole('dialog', { name: 'Navigation' });
    await user.click(within(drawer).getByRole('link', { name: 'Overview' }));
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull();
    expect(screen.getByRole('main')).toHaveFocus();
  });

  it('Escape closes the menu and returns the focus to its button', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }));
    const drawer = screen.getByRole('dialog', { name: 'Navigation' });
    within(drawer).getByRole('link', { name: 'Plans' }).focus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveFocus();
  });

  it('a tap on the dimmed page closes the menu', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Layout />, { route: '/', path: '/' });
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }));
    await user.click(screen.getByTestId('modal-backdrop'));
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Open navigation' })).toHaveFocus();
  });
});
