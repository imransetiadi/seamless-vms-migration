import { act, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithApp } from '../test/utils';
import Events from './Events';

// counts the event rows a render draws: each row formats its time once (the real formatter, so the page is
// unchanged); a plain counter, as the config's restoreMocks would reset a vi.fn between tests
const drawn = vi.hoisted(() => ({ times: 0 }));
vi.mock('../lib/format', async (importOriginal) => {
  const original = await importOriginal<typeof import('../lib/format')>();
  return {
    ...original,
    formatTime: (...args: Parameters<typeof original.formatTime>) => {
      drawn.times += 1;
      return original.formatTime(...args);
    },
  };
});

describe('Events rendering', () => {
  beforeEach(() => {
    drawn.times = 0;
  });

  it('draws only the new row when a live event arrives, not every event on the page (SDD §16)', async () => {
    const { server } = renderWithApp(<Events />, { route: '/events', token: 'viewer', live: true });
    const list = await screen.findByRole('list', { name: /events, newest first/i });
    await within(list).findByText(/paused: vCenter degraded/i);
    await waitFor(() => expect(server.subscriberCount).toBeGreaterThan(0));
    const rows = within(list).getAllByRole('listitem').length;
    expect(rows).toBeGreaterThan(20);
    drawn.times = 0;

    act(() => {
      server.emit({
        kind: 'migration.phase',
        plan_id: 'plan-4f2a9c1e',
        migration_id: 'mig-3c1a0f9e23',
        actor: 'orchestrator',
        message: 'web-03: cutover → verifying (render count)',
        data: { from_phase: 'cutover', to_phase: 'verifying' },
      });
    });
    await within(list).findByText('web-03: cutover → verifying (render count)');

    // the new row, plus the live indicator's "last event" time; never the rows that were already there
    expect(drawn.times).toBeLessThanOrEqual(3);
  });
});
