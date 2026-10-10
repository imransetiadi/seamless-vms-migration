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
  it('shows the newest history first', async () => {
    renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    const items = within(list).getAllByRole('listitem');
    expect(items.length).toBeGreaterThan(20);
  });

  it('appends streamed events at the top and announces them', async () => {
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await streamReady(server);

    emitPhase(server, 'web-03: cutover → verifying (streamed)');

    expect(await within(list).findByText('web-03: cutover → verifying (streamed)')).toBeInTheDocument();
    expect(within(list).getAllByRole('listitem')[0]).toHaveTextContent('web-03: cutover → verifying (streamed)');
    expect(screen.getByRole('status', { name: /event stream/i })).toHaveTextContent(/1 new event/i);
  });

  it('holds new events while paused and shows them on resume', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await streamReady(server);

    // Scope button queries to the page header: role queries over ~160 rendered events are slow.
    const header = screen.getByRole('heading', { level: 1, name: 'Events' }).closest('header') as HTMLElement;
    await user.click(within(header).getByRole('button', { name: /pause live updates/i }));
    emitPhase(server, 'held while paused');
    expect(await within(header).findByRole('button', { name: /resume \(1 new\)/i })).toBeInTheDocument();
    expect(within(list).queryByText('held while paused')).not.toBeInTheDocument();

    await user.click(within(header).getByRole('button', { name: /resume \(1 new\)/i }));
    expect(await within(list).findByText('held while paused')).toBeInTheDocument();
  });

  it('ignores replayed events it already has (a server that replays history on connect)', async () => {
    const hub = new LiveEventHub();
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
    expect(screen.getByRole('status', { name: /event stream/i })).toHaveTextContent(/waiting for new events/i);
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

  it('filters by category and text', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);

    await user.selectOptions(screen.getByLabelText('Category'), 'security');
    const items = within(list).getAllByRole('listitem');
    expect(items.length).toBeGreaterThan(0);
    for (const item of items) expect(item).toHaveTextContent(/auth\.denied/);

    await user.selectOptions(screen.getByLabelText('Category'), 'all');
    await user.type(screen.getByRole('searchbox', { name: /search events/i }), 'legacy-rhel6-app');
    for (const item of within(list).getAllByRole('listitem')) expect(item).toHaveTextContent(/legacy-rhel6-app/);
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
      expect(lines.every((e) => e.kind.startsWith('auth.'))).toBe(true);
      expect(lines.map((e) => e.seq)).toEqual([...lines.map((e) => e.seq)].sort((a, b) => a - b));
      expect(blobs[0]!.type).toBe('application/x-ndjson');
    } finally {
      create.mockRestore();
      revoke.mockRestore();
      click.mockRestore();
    }
  });
});
