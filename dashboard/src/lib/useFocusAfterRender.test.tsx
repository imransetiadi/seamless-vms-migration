import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useFocusAfterRender } from './useFocusAfterRender';

function Swap({ same = false }: { same?: boolean }) {
  const [shown, setShown] = useState(same);
  const ref = useRef<HTMLHeadingElement>(null);
  const focusAfterRender = useFocusAfterRender();
  return (
    <div>
      {shown && (
        <h2 ref={ref} tabIndex={-1}>
          Result
        </h2>
      )}
      <button
        onClick={() => {
          setShown(true);
          focusAfterRender(() => ref.current);
        }}
      >
        Show
      </button>
    </div>
  );
}

describe('useFocusAfterRender (SDD §16)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('focuses the element the update draws, even when a frame runs before the commit', async () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(performance.now());
      return 0;
    });
    const user = userEvent.setup({ delay: null });
    render(<Swap />);
    await user.click(screen.getByRole('button', { name: 'Show' }));
    expect(screen.getByRole('heading', { name: 'Result' })).toHaveFocus();
  });

  it('still focuses on the next frame when the update draws nothing new', async () => {
    const user = userEvent.setup({ delay: null });
    render(<Swap same />);
    await user.click(screen.getByRole('button', { name: 'Show' }));
    await vi.waitFor(() => expect(screen.getByRole('heading', { name: 'Result' })).toHaveFocus());
  });
});
