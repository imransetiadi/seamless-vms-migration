import { Link, useLocation, useNavigate } from 'react-router-dom';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LiveContext, LiveEventHub } from '../api/live';
import type { MockServer } from '../api/mock';
import { createTestServer, renderWithApp } from '../test/utils';
import Events from './Events';

async function streamReady(server: MockServer) {
  await waitFor(() => expect(server.subscriberCount).toBeGreaterThan(0));
}

function emitPhase(server: MockServer, message: string) {
  act(() => {
    server.emit({
      kind: 'migration.phase',
      plan_id: 'plan-4f2a9c1e',
      migration_id: 'mig-3c1a0f9e23',
      actor: 'orchestrator',
      message,
      data: { from_phase: 'cutover', to_phase: 'verifying' },
    });
  });
}

describe('Events page', () => {
  it('keeps manually loaded older pages after leaving and going Back', async () => {
    function Journey() {
      const location = useLocation(); const navigate = useNavigate();
      return location.pathname === '/events' ? <><Events /><Link to="/plans">Leave older list</Link></> : <button onClick={() => navigate(-1)}>Back older list</button>;
    }
    const server = createTestServer();
    server.emit({ kind: 'policy.updated', plan_id: null, migration_id: null, actor: 'test', message: 'retained older record', data: {} });
    for (let i = 0; i < 501; i++) server.emit({ kind: 'policy.updated', plan_id: null, migration_id: null, actor: 'test', message: 'newer record', data: {} });
    renderWithApp(<Journey />, { route: '/events', server });
    const user = userEvent.setup({ delay: null });
    await screen.findByRole('list', { name: /events, newest first/i });
    await user.type(screen.getByLabelText('Search events'), 'retained older record');
    await user.click(screen.getByRole('button', { name: 'Load older events' }));
    expect(await screen.findByText('retained older record')).toBeInTheDocument();
    await user.click(screen.getByText('Leave older list'));
    await user.click(screen.getByText('Back older list'));
    expect(screen.getByText('retained older record')).toBeInTheDocument();
  });
  it('restores filters and Pause after leaving and going Back', async () => {
    function Journey() {
      const location = useLocation();
      const navigate = useNavigate();
      return location.pathname === '/events' ? <><Events /><Link to="/plans">Leave list</Link></> : <button onClick={() => navigate(-1)}>Back</button>;
    }
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Journey />, { route: '/events' });
    await screen.findByRole('list', { name: /events, newest first/i });
    await user.type(screen.getByLabelText('Search events'), 'web-01');
    await user.selectOptions(screen.getByLabelText('Category'), 'migrations');
    await user.click(screen.getByRole('button', { name: 'Pause live updates' }));
    await user.click(screen.getByText('Leave list'));
    await user.click(screen.getByText('Back'));
    expect(screen.getByLabelText('Search events')).toHaveValue('web-01');
    expect(screen.getByLabelText('Category')).toHaveValue('migrations');
    expect(screen.getByRole('button', { name: 'Resume live updates' })).toBeInTheDocument();
  });
  it.each([['connecting', 'Connecting'], ['reconnecting', 'Reconnecting'], ['closed', 'Offline'], ['unauthorized', 'Signed out']] as const)('does not claim Live while %s, and recovers', async (status, label) => {
    const hub = new LiveEventHub(); hub.setStatus(status);
    renderWithApp(<LiveContext.Provider value={hub}><Events /></LiveContext.Provider>, { route: '/events' });
    expect(await screen.findByText(`${label} · showing last loaded events`)).toBeInTheDocument();
    expect(screen.queryByText('Live · waiting for new events')).not.toBeInTheDocument();
    act(() => hub.setStatus('open'));
    expect(screen.getByText('Live · waiting for new events')).toBeInTheDocument();
  });
  it('returns focus to Search events when filters hide the list and the last older page removes its button', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 500; i += 1) {
      server.emit({ kind: 'policy.updated', plan_id: null, migration_id: null, actor: 'rina', message: `policy edit ${i}`, data: {} });
    }
    renderWithApp(<Events />, { route: '/events', server });
    await screen.findByRole('list', { name: /events, newest first/i });
    const search = screen.getByLabelText('Search events');
    await user.type(search, 'no-matching-event');
    await user.click(screen.getByRole('button', { name: 'Load older events' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load older events' })).not.toBeInTheDocument());
    await waitFor(() => expect(search).toHaveFocus());
  });

  it('keeps focus on Search events when the final older request completes after the user moved there', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 500; i += 1) {
      server.emit({ kind: 'policy.updated', plan_id: null, migration_id: null, actor: 'rina', message: `policy edit ${i}`, data: {} });
    }
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    renderWithApp(<Events />, {
      route: '/events', server,
      wrapFetch: (mock) => async (input, init) => {
        if (new URL(String(input), 'http://localhost').searchParams.has('before')) await pending;
        return mock(input, init);
      },
    });
    await screen.findByRole('list', { name: /events, newest first/i });
    const search = screen.getByLabelText('Search events');
    await user.type(search, 'policy edit 1');
    await user.click(screen.getByRole('button', { name: 'Load older events' }));
    await user.clear(search);
    await user.type(search, 'policy edit 2');
    release();
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load older events' })).not.toBeInTheDocument());
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(search).toHaveFocus();
  });

  it('says the plans are unavailable when the plan filter cannot list them (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/plans'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/plans are unavailable/i);
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows the newest history first', async () => {
    renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    const items = within(list).getAllByRole('listitem');
    expect(items.length).toBeGreaterThan(20);
  });

  it('appends streamed events at the top and counts them without announcing each (SDD §16)', async () => {
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await streamReady(server);

    emitPhase(server, 'web-03: cutover → verifying (streamed)');

    expect(await within(list).findByText('web-03: cutover → verifying (streamed)')).toBeInTheDocument();
    expect(within(list).getAllByRole('listitem')[0]).toHaveTextContent('web-03: cutover → verifying (streamed)');
    // the count is plain text: a live region would read every event out, flooding a screen reader (WCAG 4.1.3)
    const count = screen.getByText('Live · 1 new event since you opened this page');
    expect(count.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
  });

  it('holds new events while paused and shows them on resume', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await streamReady(server);

    // Scope button queries to the page header: role queries over ~160 rendered events are slow.
    const header = screen.getByRole('heading', { level: 1, name: 'Events' }).closest('header') as HTMLElement;
    const regions = screen.getAllByRole('status');
    await user.click(within(header).getByRole('button', { name: 'Pause live updates' }));
    expect(regions).toContain(screen.getByText('Live updates paused: new events wait until you resume.'));
    emitPhase(server, 'held while paused');
    expect(within(list).queryByText('held while paused')).not.toBeInTheDocument();
    // Pause stops everything that updates on its own, the count and the button's label too (WCAG 2.2.2)
    expect(screen.getByText('Paused · new events wait until you resume')).toBeInTheDocument();
    const resume = within(header).getByRole('button', { name: 'Resume live updates' });

    await user.click(resume);
    expect(await within(list).findByText('held while paused')).toBeInTheDocument();
    expect(regions).toContain(screen.getByText('Live updates resumed: 1 held event added.'));
  });

  it('ignores replayed events it already has (a server that replays history on connect)', async () => {
    const hub = new LiveEventHub();
    hub.setStatus('open');
    const { server } = renderWithApp(
      <LiveContext.Provider value={hub}>
        <Events />
      </LiveContext.Provider>,
      { route: '/events', token: 'viewer' },
    );
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    const before = within(list).getAllByRole('listitem').length;
    const replayed = server.events[server.events.length - 1];
    if (!replayed) throw new Error('no fixture events');

    act(() => hub.publish({ ...replayed }));

    expect(within(list).getAllByRole('listitem')).toHaveLength(before);
    expect(screen.getByText('Live · waiting for new events')).toBeInTheDocument();
  });

  it('shows a new event the stream delivers twice once (SDD §16, perf audit PF-07)', async () => {
    const hub = new LiveEventHub();
    hub.setStatus('open');
    const { server } = renderWithApp(
      <LiveContext.Provider value={hub}>
        <Events />
      </LiveContext.Provider>,
      { route: '/events', token: 'viewer' },
    );
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    const last = server.events[server.events.length - 1];
    if (!last) throw new Error('no fixture events');
    // newer than the history: only the page's own record of what it showed tells a second delivery
    const fresh = { ...last, seq: last.seq + 1, message: 'zz delivered twice' };
    act(() => hub.publish({ ...fresh }));
    act(() => hub.publish({ ...fresh }));
    expect(await within(list).findAllByText('zz delivered twice')).toHaveLength(1);
    // and counted once on the stream line
    expect(screen.getByText('Live · 1 new event since you opened this page')).toBeInTheDocument();
  });

  it('never says no events match when the history cannot be loaded (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/events'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/the audit trail is unavailable/i);
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryAllByText(/no events match/i)).toHaveLength(0);
    // the download says why there is nothing to download
    expect(screen.getByRole('button', { name: /download shown events/i })).toHaveAccessibleDescription(/audit trail could not be loaded/i);
  });

  it('shows the audit trail of one plan when it is chosen, live events too (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await streamReady(server);
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const before = within(list).getAllByRole('listitem').length;
    await user.selectOptions(screen.getByLabelText('Plan'), plan.id);
    // only that plan's events (the list is drawn again from the plan's history): each one links to it
    const shown = () => within(screen.getByRole('list', { name: /events, newest first/i })).getAllByRole('listitem');
    await waitFor(() => {
      const items = shown();
      expect(items.length).toBeGreaterThan(0);
      expect(items.length).toBeLessThan(before);
      for (const item of items) expect(within(item).getByRole('link', { name: plan.name })).toBeInTheDocument();
    });
    // a live event of another plan stays out, one of this plan comes in
    act(() => {
      server.emit({ kind: 'plan.updated', plan_id: 'plan-c81d44a0', migration_id: null, actor: 'rina', message: 'zz other plan event', data: {} });
      server.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: 'rina', message: 'zz this plan event', data: {} });
    });
    expect(await screen.findByText('zz this plan event')).toBeInTheDocument();
    expect(screen.queryByText('zz other plan event')).not.toBeInTheDocument();
  });

  it('opens on the plan in the address and loads only its history (SDD §16)', async () => {
    const server = createTestServer();
    const asked: string[] = [];
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      if (method === 'GET' && path === '/events') asked.push(query.get('plan_id') ?? '');
      return handle(method, path, query, body, token);
    };
    renderWithApp(<Events />, { route: '/events?plan=plan-4f2a9c1e', token: 'viewer', server });
    await screen.findByRole('list', { name: /events, newest first/i });
    await waitFor(() => expect(screen.getByLabelText('Plan')).toHaveValue('plan-4f2a9c1e'));
    expect(asked).toContain('plan-4f2a9c1e');
  });

  it('says from a live region already on the page when its filters match no event (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    const regions = screen.getAllByRole('status');
    await user.type(screen.getByRole('searchbox', { name: /search events/i }), 'zzzz-no-match');
    const none = await screen.findByText('No events match');
    expect(regions).toContain(none.closest('[role="status"]'));
  });

  it('filters by category and text', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);

    await user.selectOptions(screen.getByLabelText('Category'), 'security');
    const items = within(list).getAllByRole('listitem');
    expect(items.length).toBeGreaterThan(0);
    // Security: access refused, and the advisor policies' changes (review RV13-05)
    for (const item of items) expect(item).toHaveTextContent(/auth\.denied|policy\.(created|updated|deleted)/);

    await user.selectOptions(screen.getByLabelText('Category'), 'all');
    await user.type(screen.getByRole('searchbox', { name: /search events/i }), 'legacy-rhel6-app');
    for (const item of within(list).getAllByRole('listitem')) expect(item).toHaveTextContent(/legacy-rhel6-app/);
  });

  it('clears search, category, plan and progress filters without resuming paused updates', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Events />, { route: '/events?plan=plan-4f2a9c1e', token: 'viewer', live: true });
    await screen.findByRole('list', { name: /events, newest first/i });
    await user.click(screen.getByRole('button', { name: 'Pause live updates' }));
    await user.selectOptions(screen.getByLabelText('Category'), 'security');
    await user.click(screen.getByLabelText('Show progress updates'));
    await user.type(screen.getByLabelText('Search events'), 'no-match');
    expect(screen.getByText('No events match')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Search events')).toHaveValue('');
    expect(screen.getByLabelText('Search events')).toHaveFocus();
    expect(screen.getByLabelText('Category')).toHaveValue('all');
    expect(screen.getByLabelText('Plan')).toHaveValue('');
    expect(screen.getByLabelText('Show progress updates')).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Resume live updates' })).toBeInTheDocument();
    await screen.findByRole('list', { name: /events, newest first/i });
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument();
  });

  it('files a prune of the audit trail under Security (SDD §4.3, §15)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    server.emit({
      kind: 'events.pruned',
      plan_id: null,
      migration_id: null,
      actor: 'cli',
      message: '120 event(s) before 2026-09-01T00:00:00+00:00 deleted from the CLI',
      data: { before: '2026-09-01T00:00:00+00:00', deleted: 120 },
    });
    renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/120 event\(s\) before/);

    await user.selectOptions(screen.getByLabelText('Category'), 'security');
    const items = within(list).getAllByRole('listitem');
    expect(items.some((item) => item.textContent?.includes('events.pruned'))).toBe(true);
    for (const item of items) expect(item).toHaveTextContent(/auth\.denied|events\.pruned|policy\./);
  });

  it('files a dashboard user created under Security: it grants access (SDD §4.3, §13.1, review RV12-04)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    server.emit({
      kind: 'user.created',
      plan_id: null,
      migration_id: null,
      actor: 'rina',
      message: 'User mallory created',
      data: { name: 'mallory', role: 'admin' },
    });
    renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText('User mallory created');

    await user.selectOptions(screen.getByLabelText('Category'), 'security');
    const items = within(list).getAllByRole('listitem');
    expect(items.filter((item) => item.textContent?.includes('user.created'))).toHaveLength(1);
    for (const item of items) expect(item).toHaveTextContent(/auth\.denied|events\.pruned|user\.created|policy\./);
  });

  it('moves focus to the list when its last Show older events button goes, never to the page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 250; i++) {
      server.emit({ kind: 'plan.updated', plan_id: 'plan-4f2a9c1e', migration_id: null, actor: 'rina', message: `bulk change ${i}`, data: {} });
    }
    renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText('bulk change 249');
    let clicks = 0;
    for (let more = screen.queryByRole('button', { name: /show \d+ older events/i }); more; more = screen.queryByRole('button', { name: /show \d+ older events/i })) {
      await user.click(more);
      clicks += 1;
      // while the button stays, it keeps the focus; once it goes, the list takes it
      expect(screen.queryByRole('button', { name: /show \d+ older events/i }) ?? list).toHaveFocus();
    }
    expect(clicks).toBeGreaterThan(1);
  });

  it('downloads only the events shown, not the older ones behind Show older events (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 250; i++) {
      server.emit({ kind: 'plan.updated', plan_id: 'plan-4f2a9c1e', migration_id: null, actor: 'rina', message: `bulk change ${i}`, data: {} });
    }
    const blobs: Blob[] = [];
    const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      blobs.push(b as Blob);
      return 'blob:events';
    });
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const lineCount = async (blob: Blob) =>
      (await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blob);
      }))
        .trim()
        .split('\n').length;
    try {
      renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
      const list = await screen.findByRole('list', { name: /events, newest first/i });
      await within(list).findByText('bulk change 249');
      const more = screen.getByRole('button', { name: /show \d+ older events/i });
      const shown = within(list).getAllByRole('listitem').length;

      await user.click(screen.getByRole('button', { name: /download shown events/i }));
      expect(await lineCount(blobs[0]!)).toBe(shown);

      // showing older events adds them to the download
      await user.click(more);
      const shownNow = within(list).getAllByRole('listitem').length;
      expect(shownNow).toBeGreaterThan(shown);
      await user.click(screen.getByRole('button', { name: /download shown events/i }));
      expect(await lineCount(blobs[1]!)).toBe(shownNow);
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('says why there is nothing to download when only progress updates are shown (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await streamReady(server);
    // the page keeps progress updates only while they are shown
    await user.click(screen.getByRole('checkbox', { name: /show progress updates/i }));
    act(() => {
      // progress updates are streamed, never persisted (seq 0)
      server.emit({ kind: 'migration.progress', plan_id: 'plan-4f2a9c1e', migration_id: 'mig-3c1a0f9e23', actor: 'orchestrator', message: 'zz-probe 42%', data: {} }, false);
    });
    await user.type(screen.getByRole('searchbox', { name: /search events/i }), 'zz-probe');
    await within(list).findByText('zz-probe 42%');
    const download = screen.getByRole('button', { name: /download shown events/i });
    expect(download).toHaveAttribute('aria-disabled', 'true');
    expect(download).toHaveAccessibleDescription('Only progress updates are shown; they are not part of the audit trail.');
  });

  it('puts a streamed progress update at the top by its time, though it has no sequence number', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await streamReady(server);
    await user.click(screen.getByRole('checkbox', { name: /show progress updates/i }));
    act(() => {
      // progress updates are streamed, never persisted (seq 0): only their time places them
      server.emit({ kind: 'migration.progress', plan_id: 'plan-4f2a9c1e', migration_id: 'mig-3c1a0f9e23', actor: 'orchestrator', message: 'web-03 61%', data: {} }, false);
    });
    await within(list).findByText('web-03 61%');
    expect(within(list).getAllByRole('listitem')[0]).toHaveTextContent('web-03 61%');
  });

  it('downloads the shown events as JSON lines, oldest first, like `seamless events export`', async () => {
    const user = userEvent.setup({ delay: null });
    const blobs: Blob[] = [];
    const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      blobs.push(b as Blob);
      return 'blob:events';
    });
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
      const list = await screen.findByRole('list', { name: /events, newest first/i });
      await within(list).findByText(/paused: vCenter degraded/i);
      await user.selectOptions(screen.getByLabelText('Category'), 'security');
      const shown = within(list).getAllByRole('listitem').length;

      await user.click(screen.getByRole('button', { name: /download shown events/i }));
      expect(click).toHaveBeenCalledTimes(1);
      const text = await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blobs[0]!);
      });
      const lines = text.trim().split('\n').map((l) => JSON.parse(l) as { seq: number; kind: string });
      expect(lines).toHaveLength(shown);
      expect(lines.every((e) => e.kind.startsWith('auth.') || e.kind.startsWith('policy.'))).toBe(true);
      expect(lines.map((e) => e.seq)).toEqual([...lines.map((e) => e.seq)].sort((a, b) => a - b));
      expect(blobs[0]!.type).toBe('application/x-ndjson');
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('exports the whole trail, every persisted event and not only the shown ones, like `seamless events export` (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const blobs: Blob[] = [];
    const parts: number[] = [];
    const RealBlob = globalThis.Blob;
    vi.stubGlobal(
      'Blob',
      class extends RealBlob {
        constructor(content?: BlobPart[], options?: BlobPropertyBag) {
          super(content, options);
          parts.push(content?.length ?? 0);
        }
      },
    );
    const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      blobs.push(b as Blob);
      return 'blob:events';
    });
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer' });
      const list = await screen.findByRole('list', { name: /events, newest first/i });
      await within(list).findByText(/paused: vCenter degraded/i);
      await user.selectOptions(screen.getByLabelText('Category'), 'security');
      const regions = screen.getAllByRole('status');

      const exportButton = screen.getByRole('button', { name: 'Export the whole trail' });
      await user.click(exportButton);
      const persisted = server.events.filter((e) => e.seq > 0);
      // the result is said once, from a region already on the page; focus stays on the button
      const said = await screen.findByText(`Exported ${persisted.length} events.`);
      expect(regions).toContain(said.closest('[role="status"]'));
      expect(exportButton).toHaveFocus();
      expect(click).toHaveBeenCalledTimes(1);
      const text = await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blobs[0]!);
      });
      const seqs = text.trim().split('\n').map((l) => (JSON.parse(l) as { seq: number }).seq);
      expect(seqs).toEqual(persisted.map((e) => e.seq));
      expect(blobs[0]!.type).toBe('application/x-ndjson');
      // built from one part per event, never one string: millions of events exceed a string's maximum length
      expect(parts.at(-1)).toBe(persisted.length);
    } finally {
      vi.unstubAllGlobals();
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('stops an export from the same button, saves nothing and says so', async () => {
    const user = userEvent.setup({ delay: null });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      // the export's paging request stays open until it is aborted
      const held: typeof fetch = (_input, init) =>
        new Promise((_, reject) => init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));
      renderWithApp(<Events />, {
        route: '/events',
        token: 'viewer',
        wrapFetch: (mock) => (input, init) => (String(input).includes('since=') ? held(input, init) : mock(input, init)),
      });
      await screen.findByRole('list', { name: /events, newest first/i });
      const button = screen.getByRole('button', { name: 'Export the whole trail' });
      await user.click(button);
      expect(await screen.findByText(/Exporting the whole trail: 0 events read/)).toBeInTheDocument();
      expect(button).toHaveAccessibleName('Stop export');
      await user.click(button);
      expect(await screen.findByText('Export stopped: nothing was saved.')).toBeInTheDocument();
      expect(button).toHaveAccessibleName('Export the whole trail');
      expect(button).toHaveFocus();
      expect(click).not.toHaveBeenCalled();
      expect(screen.queryByText(/The export failed/)).not.toBeInTheDocument();
    } finally {
      click.mockRestore();
    }
  });

  it('stops a running export when the page is left', async () => {
    const user = userEvent.setup({ delay: null });
    let aborted = false;
    const held: typeof fetch = (_input, init) =>
      new Promise((_, reject) =>
        init?.signal?.addEventListener('abort', () => {
          aborted = true;
          reject(new DOMException('aborted', 'AbortError'));
        }),
      );
    const { unmount } = renderWithApp(<Events />, {
      route: '/events',
      token: 'viewer',
      wrapFetch: (mock) => (input, init) => (String(input).includes('since=') ? held(input, init) : mock(input, init)),
    });
    await screen.findByRole('list', { name: /events, newest first/i });
    await user.click(screen.getByRole('button', { name: 'Export the whole trail' }));
    await screen.findByText(/Exporting the whole trail/);
    unmount();
    await waitFor(() => expect(aborted).toBe(true));
  });

  it('exports only the chosen plan’s trail when a plan is chosen', async () => {
    const user = userEvent.setup({ delay: null });
    const blobs: Blob[] = [];
    const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      blobs.push(b as Blob);
      return 'blob:events';
    });
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      const { server } = renderWithApp(<Events />, { route: '/events?plan=plan-4f2a9c1e', token: 'viewer' });
      await screen.findByRole('list', { name: /events, newest first/i });
      await user.click(screen.getByRole('button', { name: 'Export the whole trail' }));
      const mine = server.events.filter((e) => e.seq > 0 && e.plan_id === 'plan-4f2a9c1e');
      await screen.findByText(`Exported ${mine.length} events.`);
      const text = await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blobs[0]!);
      });
      expect(text.trim().split('\n').map((l) => (JSON.parse(l) as { seq: number }).seq)).toEqual(mine.map((e) => e.seq));
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });

  it('loads the events before the oldest shown once every loaded one is shown, from the same button (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 900; i += 1) {
      server.emit({ kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `bulk check ${i}`, data: {} });
    }
    const persisted = server.events.filter((e) => e.seq > 0);
    expect(persisted.length).toBeGreaterThan(1000); // the loaded 500, a full page before them, and the rest
    const asked: string[] = [];
    renderWithApp(<Events />, {
      route: '/events',
      token: 'viewer',
      server,
      wrapFetch: (mock) => (input, init) => {
        const url = new URL(String(input), 'http://x');
        if (url.pathname.endsWith('/events') && url.searchParams.has('before')) asked.push(url.searchParams.get('before')!);
        return mock(input, init);
      },
    });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(200));
    const more = screen.getByRole('button', { name: 'Show 200 older events' });
    await user.click(more);
    await user.click(more);
    // the 500 loaded are all shown: the same button now reads the page before them
    expect(more).toHaveAccessibleName('Load older events');
    await user.click(more);
    expect(asked).toEqual([String(persisted[persisted.length - 500]!.seq)]);
    expect(await screen.findByText('Loaded 500 older events.')).toBeInTheDocument();
    // a full page: there may be more, so the button stays — showing the next 200 — and keeps the focus
    expect(more).toHaveAccessibleName('Show 200 older events');
    expect(more).toHaveFocus();
    expect(within(list).getAllByRole('listitem')).toHaveLength(800);

    await user.click(more);
    expect(more).toHaveAccessibleName('Load older events');
    await user.click(more);
    const rest = persisted.length - 1000;
    expect(await screen.findByText(`Loaded ${rest} older events.`)).toBeInTheDocument();
    expect(asked).toEqual([String(persisted[persisted.length - 500]!.seq), String(persisted[persisted.length - 1000]!.seq)]);
    // the start of the trail: every event is shown, the button goes and the list takes the focus (SDD §16)
    await waitFor(() => expect(list).toHaveFocus());
    expect(screen.queryByRole('button', { name: /older events/ })).not.toBeInTheDocument();
    expect(within(list).getAllByRole('listitem')).toHaveLength(persisted.length);
    expect(within(list).getByText(persisted[0]!.message)).toBeInTheDocument();
  });

  it('keeps every loaded event when the newest history is read again after older pages were loaded', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    for (let i = 0; i < 400; i += 1) {
      server.emit({ kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `bulk check ${i}`, data: {} });
    }
    const before = server.events.filter((e) => e.seq > 0);
    const { queryClient } = renderWithApp(<Events />, { route: '/events', token: 'viewer', server });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(200));
    const more = screen.getByRole('button', { name: 'Show 200 older events' });
    await user.click(more);
    await user.click(more);
    await user.click(more); // Load older events: the rest of the trail
    await screen.findByText(`Loaded ${before.length - 500} older events.`);
    // new events, then the newest history is read again (as an audit event makes the page do)
    for (let i = 0; i < 3; i += 1) {
      server.emit({ kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `late check ${i}`, data: {} });
    }
    await act(() => queryClient.invalidateQueries({ queryKey: ['events', 'tail'] }));
    await within(list).findByText('late check 2');
    while (screen.queryByRole('button', { name: /older events/ })) await user.click(screen.getByRole('button', { name: /older events/ }));
    // the oldest events of the earlier window are still there: no gap between the history and the older pages
    expect(within(list).getAllByRole('listitem')).toHaveLength(before.length + 3);
  });

  it('offers no Load older events when the loaded history is the whole trail', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer' });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    expect(server.events.filter((e) => e.seq > 0).length).toBeLessThan(500);
    while (screen.queryByRole('button', { name: /^Show \d+ older events$/ })) await user.click(screen.getByRole('button', { name: /^Show \d+ older events$/ }));
    expect(within(list).getAllByRole('listitem').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Load older events' })).not.toBeInTheDocument();
  });

  it('lists the audit events under Advisor and memory (SDD §14.5)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Events />, { route: '/events', token: 'viewer' });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await user.selectOptions(screen.getByLabelText('Category'), 'advisor');
    expect(await within(list).findByText('static-cdn-01: decision cold (below_threshold) by policy:global-default')).toBeInTheDocument();
    expect(within(list).getByText('static-cdn-01: recommended warm (57%)')).toBeInTheDocument();
  });
});
