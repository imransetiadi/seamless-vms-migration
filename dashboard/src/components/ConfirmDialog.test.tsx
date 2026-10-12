import { render, screen, within } from '@testing-library/react';
import { Trash2 } from 'lucide-react';
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

  it('keeps Cancel focusable while its request runs, saying why (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const onCancel = vi.fn();
    render(<ConfirmDialog open tone="danger" title="Finalize web-01?" confirmLabel="Finalize" pending onConfirm={vi.fn()} onCancel={onCancel} />);
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    // a danger dialog opens on Cancel: it keeps the focus while the request runs
    expect(cancel).toHaveFocus();
    expect(cancel).not.toBeDisabled();
    expect(cancel).toHaveAttribute('aria-disabled', 'true');
    expect(cancel).toHaveAccessibleDescription('Wait for the request to finish.');
    await user.click(cancel);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('still says why when the caller gives no reason of its own', () => {
    render(<ConfirmDialog open title="Plan waves?" confirmLabel="Plan waves" canConfirm={false} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Plan waves' })).toHaveAccessibleDescription('Complete the fields above first.');
  });

  it('shows the tone icon, the object and what happens, and names the action with its icon (SDD §16)', () => {
    render(
      <ConfirmDialog
        open
        tone="danger"
        icon={Trash2}
        title="Delete RHOSP 17.1 — DC1?"
        subject="Provider rhosp17-dc1"
        consequences={[
          { kind: 'removed', text: 'the provider, its stored sign-in details and SSH key' },
          { kind: 'kept', text: 'the cloud itself' },
          { kind: 'then', text: 'plans that use it keep it until their VMs are finalized' },
        ]}
        confirmLabel="Delete provider"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const dialog = screen.getByRole('alertdialog', { name: 'Delete RHOSP 17.1 — DC1?' });
    expect(within(dialog).getByTestId('confirm-tone-icon')).toHaveAttribute('data-tone', 'danger');
    expect(within(dialog).getByText('Provider rhosp17-dc1')).toBeInTheDocument();
    const rows = within(within(dialog).getByRole('list', { name: 'What happens' })).getAllByRole('listitem');
    expect(rows.map((row) => row.textContent)).toEqual([
      'Removed the provider, its stored sign-in details and SSH key',
      'Kept the cloud itself',
      'Then plans that use it keep it until their VMs are finalized',
    ]);
    // the dialog is described by what happens: read out when it opens
    expect(dialog).toHaveAccessibleDescription(/Removed the provider, its stored sign-in details and SSH key.*Kept the cloud itself/);
    const confirm = within(dialog).getByRole('button', { name: 'Delete provider' });
    expect(confirm.querySelector('svg')).not.toBeNull();
    // the actions stand in the footer, Cancel first
    const footer = within(dialog).getByTestId('confirm-actions');
    expect(within(footer).getAllByRole('button').map((b) => b.textContent)).toEqual(['Cancel', 'Delete provider']);
  });

  it('a default dialog shows a neutral icon; one without consequences is described by its description', () => {
    render(<ConfirmDialog open title="Pause this plan?" description="Running steps finish first." confirmLabel="Pause plan" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const dialog = screen.getByRole('alertdialog', { name: 'Pause this plan?' });
    expect(within(dialog).getByTestId('confirm-tone-icon')).toHaveAttribute('data-tone', 'default');
    expect(dialog).toHaveAccessibleDescription('Running steps finish first.');
    expect(within(dialog).queryByRole('list', { name: 'What happens' })).toBeNull();
  });

  it('the close button does what Cancel does, and says why it is unavailable while the request runs', async () => {
    const user = userEvent.setup({ delay: null });
    const onCancel = vi.fn();
    const { rerender } = render(<ConfirmDialog open tone="danger" title="Delete x?" confirmLabel="Delete" onConfirm={vi.fn()} onCancel={onCancel} />);
    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    rerender(<ConfirmDialog open tone="danger" title="Delete x?" confirmLabel="Delete" pending onConfirm={vi.fn()} onCancel={onCancel} />);
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close).toHaveAttribute('aria-disabled', 'true');
    expect(close).toHaveAccessibleDescription('Wait for the request to finish.');
    await user.click(close);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('says when the typed text matches', async () => {
    const user = userEvent.setup({ delay: null });
    render(<ConfirmDialog open tone="danger" title="Delete x?" confirmLabel="Delete" requireText="rhosp17-dc1" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const status = screen.getByTestId('confirm-match');
    expect(status).toHaveTextContent('');
    await user.type(screen.getByLabelText(/type rhosp17-dc1 to confirm/i), 'rhosp17-dc1');
    expect(status).toHaveTextContent('Matches');
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
