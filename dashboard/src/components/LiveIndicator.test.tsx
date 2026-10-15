import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LiveContext, LiveEventHub } from '../api/live';
import { LiveIndicator } from './LiveIndicator';

describe('LiveIndicator', () => {
  it('gives when the last update came on focus, hover and tap - a focusable term, never only a title (SDD §16, audit C-15)', async () => {
    const user = userEvent.setup({ delay: null });
    const hub = new LiveEventHub();
    render(
      <LiveContext.Provider value={hub}>
        <LiveIndicator />
      </LiveContext.Provider>,
    );
    const indicator = screen.getByRole('button', { name: 'Event stream: Offline' });
    expect(indicator).toHaveAccessibleDescription('No updates received yet');
    expect(indicator).not.toHaveAttribute('title');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    // a heartbeat at 12:00:05 (tests run in UTC): the detail names it, and the label follows the stream
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-08T12:00:05Z'));
    act(() => {
      hub.setStatus('open');
      hub.heartbeat();
    });
    const live = screen.getByRole('button', { name: 'Event stream: Live' });
    expect(live).toHaveAccessibleDescription('Last update 12:00:05');

    await user.tab();
    expect(live).toHaveFocus();
    expect(screen.getByRole('tooltip')).toHaveTextContent('Last update 12:00:05');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    await user.hover(live);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Last update 12:00:05');
  });

  it('keeps its label visible beside the icon, and only to assistive technology in the compact header form (SDD §16)', () => {
    const hub = new LiveEventHub();
    const { unmount } = render(
      <LiveContext.Provider value={hub}>
        <LiveIndicator />
      </LiveContext.Provider>,
    );
    expect(screen.getByText('Offline')).not.toHaveClass('sr-only');
    unmount();
    render(
      <LiveContext.Provider value={hub}>
        <LiveIndicator compact />
      </LiveContext.Provider>,
    );
    expect(screen.getByRole('button', { name: 'Event stream: Offline' })).toBeInTheDocument();
    expect(screen.getByText('Offline')).toHaveClass('sr-only');
  });
});
