import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
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
});
