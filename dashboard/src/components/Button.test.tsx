import { act, fireEvent, render, screen } from '@testing-library/react';
import type { FormEvent } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Button } from './Button';

describe('Button', () => {
  afterEach(() => vi.useRealTimers());

  it('says why a soft-disabled action is unavailable when it is tapped or clicked, not only on hover', async () => {
    const user = userEvent.setup({ delay: null });
    const onClick = vi.fn();
    render(
      <Button disabledReason="Requires the approver role." onClick={onClick}>
        Approve
      </Button>,
    );
    const button = screen.getByRole('button', { name: 'Approve' });
    expect(button).toHaveAccessibleDescription('Requires the approver role.');
    expect(screen.queryByRole('note')).not.toBeInTheDocument();

    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByRole('note')).toHaveTextContent('Requires the approver role.');

    // leaving the button hides the note again
    fireEvent.blur(button);
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(button).toHaveAccessibleDescription('Requires the approver role.');
  });

  it('hides the note on Escape and when the page scrolls', () => {
    render(<Button disabledReason="Requires the operator role.">Validate</Button>);
    const button = screen.getByRole('button', { name: 'Validate' });
    fireEvent.click(button);
    expect(screen.getByRole('note')).toBeInTheDocument();
    fireEvent.keyDown(button, { key: 'Escape' });
    expect(screen.queryByRole('note')).not.toBeInTheDocument();

    fireEvent.click(button);
    expect(screen.getByRole('note')).toBeInTheDocument();
    fireEvent.scroll(window);
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('hides the note after a few seconds', () => {
    vi.useFakeTimers();
    render(<Button disabledReason="The plan is already running.">Start</Button>);
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    expect(screen.getByRole('note')).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('stays focusable while its request runs: busy and aria-disabled, not natively disabled (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Validate
      </Button>,
    );
    const button = screen.getByRole('button', { name: 'Validate' });
    // a natively disabled button loses keyboard focus to the page in the browser
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('aria-busy', 'true');
    button.focus();
    expect(button).toHaveFocus();

    await user.click(button);
    await user.keyboard('{Enter}');
    expect(onClick).not.toHaveBeenCalled();
    expect(button).toHaveFocus();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('does not submit its form again while its request runs (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <label>
          Name <input />
        </label>
        <Button type="submit" loading>
          Save
        </Button>
      </form>,
    );
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await user.type(screen.getByLabelText(/name/i), 'x{Enter}');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('runs an enabled action and shows no note', async () => {
    const user = userEvent.setup({ delay: null });
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Validate</Button>);
    await user.click(screen.getByRole('button', { name: 'Validate' }));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});
