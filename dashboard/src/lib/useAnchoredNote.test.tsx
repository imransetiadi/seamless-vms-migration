import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useAnchoredNote } from './useAnchoredNote';

function Note({ hideAfterMs }: { hideAfterMs?: number }) {
  const { at, noteRef, show } = useAnchoredNote(hideAfterMs);
  return (
    <>
      <button type="button" onClick={(event) => show(event.currentTarget)}>
        Show
      </button>
      <span ref={noteRef} role="note" hidden={!at} style={at ?? undefined}>
        the note
      </span>
    </>
  );
}

const rect = (r: Partial<DOMRect>): DOMRect => ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}), ...r }) as DOMRect;

describe('useAnchoredNote', () => {
  const viewport = { width: window.innerWidth, height: window.innerHeight };
  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: viewport.width });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: viewport.height });
    vi.useRealTimers();
  });

  it('places the note under its anchor, flips it above when it would overflow the bottom, and keeps it inside the sides (SDD §16, UI-03)', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 400 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 600 });
    render(<Note />);
    const button = screen.getByRole('button', { name: 'Show' });
    const note = screen.getByRole('note', { hidden: true });
    const anchor = vi.spyOn(button, 'getBoundingClientRect');
    const box = vi.spyOn(note, 'getBoundingClientRect');

    // room below: under the anchor, 6 px apart, left-aligned with it
    anchor.mockReturnValue(rect({ top: 100, bottom: 120, left: 20 }));
    box.mockReturnValue(rect({ width: 200, height: 50, top: 126, bottom: 176 }));
    fireEvent.click(button);
    expect(note).toBeVisible();
    expect(note).toHaveStyle({ top: '126px', left: '20px' });

    // near the bottom right: flipped above the anchor, and no further right than the widest note fits
    anchor.mockReturnValue(rect({ top: 560, bottom: 580, left: 300 }));
    box.mockReturnValue(rect({ width: 200, height: 50, top: 586, bottom: 636 }));
    fireEvent.click(button);
    expect(note).toHaveStyle({ top: '504px', left: '104px' });

    // the page moving dismisses it; scrolling inside the note does not
    fireEvent.scroll(note);
    expect(note).toBeVisible();
    fireEvent.scroll(window);
    expect(note).not.toBeVisible();
  });

  it('hides the note after the given time', () => {
    vi.useFakeTimers();
    render(<Note hideAfterMs={4000} />);
    fireEvent.click(screen.getByRole('button', { name: 'Show' }));
    expect(screen.getByRole('note')).toBeVisible();
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.getByRole('note', { hidden: true })).not.toBeVisible();
  });
});
