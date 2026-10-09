import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmDialog } from './ConfirmDialog';

describe('ConfirmDialog', () => {
  it('keeps Confirm focusable and says why until the typed text matches (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onConfirm = vi.fn();
    render(<ConfirmDialog open tone="danger" title="Finalize web-01?" confirmLabel="Finalize" requireText="web-01" onConfirm={onConfirm} onCancel={vi.fn()} />);
    const confirm = screen.getByRole('button', { name: 'Finalize' });
    // unavailable, yet in the Tab order, and it says why
    expect(confirm).not.toBeDisabled();
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Type web-01 to confirm.');
    for (let i = 0; i < 6 && document.activeElement !== confirm; i++) await user.tab();
    expect(confirm).toHaveFocus();
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole('note')).toHaveTextContent('Type web-01 to confirm.');

    // Enter in the field does not confirm early either
    const input = screen.getByLabelText(/type web-01 to confirm/i);
    await user.type(input, 'web-0{Enter}');
    expect(onConfirm).not.toHaveBeenCalled();
    await user.type(input, '1');
    expect(confirm).not.toHaveAttribute('aria-disabled');
    expect(confirm).not.toHaveAccessibleDescription();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('says why Confirm is unavailable while the fields of the dialog are incomplete (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog open tone="danger" title="Roll back web-01?" confirmLabel="Roll back" canConfirm={false} confirmReason="Enter a reason first." onConfirm={onConfirm} onCancel={vi.fn()} />,
    );
    const confirm = screen.getByRole('button', { name: 'Roll back' });
    expect(confirm).not.toBeDisabled();
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a reason first.');
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('still says why when the caller gives no reason of its own', () => {
    render(<ConfirmDialog open title="Plan waves?" confirmLabel="Plan waves" canConfirm={false} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Plan waves' })).toHaveAccessibleDescription('Complete the fields above first.');
  });

  it('links the mismatch message to the field it is about', async () => {
    const user = userEvent.setup({ delay: null });
    render(<ConfirmDialog open title="Delete the provider?" confirmLabel="Delete" requireText="rhosp17-dc1" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const input = screen.getByLabelText(/type rhosp17-dc1 to confirm/i);
    await user.type(input, 'rhosp');
    expect(input).toBeInvalid();
    expect(input).toHaveAccessibleDescription('The text does not match yet.');
  });
});
