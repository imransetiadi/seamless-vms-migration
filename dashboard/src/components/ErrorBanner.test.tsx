import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { recordScrolls } from '../test/utils';
import { ErrorBanner } from './ErrorBanner';

describe('ErrorBanner (SDD §16)', () => {
  let recorder: ReturnType<typeof recordScrolls> | null = null;
  afterEach(() => recorder?.restore());

  it("scrolls a dialog's request failure into view when it appears and when it changes", () => {
    recorder = recordScrolls();
    const first = new Error('secret store unavailable');
    const { rerender } = render(<ErrorBanner error={first} title="Not all changes were saved" reveal />);
    const alert = screen.getByRole('alert');
    expect(recorder.scrolls).toHaveLength(1);
    expect(recorder.scrolls[0]?.element).toBe(alert);
    expect(recorder.scrolls[0]?.options).toEqual({ block: 'nearest' });
    // drawn again with the same failure, it stays where the person scrolled it
    rerender(<ErrorBanner error={first} title="Not all changes were saved" reveal />);
    expect(recorder.scrolls).toHaveLength(1);
    // the next failure is scrolled to again
    rerender(<ErrorBanner error={new Error('secret store unavailable')} title="Not all changes were saved" reveal />);
    expect(recorder.scrolls).toHaveLength(2);
  });

  it('never scrolls to an error a page shows while loading: a refetch in the background never moves the page', () => {
    recorder = recordScrolls();
    const { rerender } = render(<ErrorBanner error={new Error('refused')} title="Plans are unavailable" />);
    rerender(<ErrorBanner error={new Error('refused again')} title="Plans are unavailable" />);
    expect(recorder.scrolls).toEqual([]);
  });

  it('keeps Retry busy while the same failure is shown, and frees it on the next failure', async () => {
    const user = userEvent.setup({ delay: null });
    const onRetry = vi.fn();
    const first = new Error('refused');
    const { rerender } = render(<ErrorBanner error={first} title="Plans are unavailable" onRetry={onRetry} />);
    await user.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: /retry/i })).toHaveAttribute('aria-busy', 'true');
    // drawn again with the same failure while the retry runs: still busy
    rerender(<ErrorBanner error={first} title="Plans are unavailable" onRetry={onRetry} />);
    expect(screen.getByRole('button', { name: /retry/i })).toHaveAttribute('aria-busy', 'true');
    // the retry failed again: that is its answer, and Retry can be pressed again
    rerender(<ErrorBanner error={new Error('refused again')} title="Plans are unavailable" onRetry={onRetry} />);
    expect(screen.getByRole('button', { name: /retry/i })).not.toHaveAttribute('aria-busy');
  });
});
