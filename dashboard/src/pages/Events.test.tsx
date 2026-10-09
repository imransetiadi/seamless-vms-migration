import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LiveContext, LiveEventHub } from '../api/live';
import type { MockServer } from '../api/mock';
import { renderWithApp } from '../test/utils';
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
