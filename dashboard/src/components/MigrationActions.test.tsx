import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Migration } from '../api/types';
import { createTestServer, renderWithApp } from '../test/utils';
import { MigrationActions } from './MigrationActions';

function setup(vmName: string, token: string) {
  const server = createTestServer();
  const migration = server.migrations.find((m) => m.vm.name === vmName) as Migration;
  const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
  const utils = renderWithApp(<MigrationActions migration={migration} plan={plan} role={token as 'viewer'} />, { server, token });
  const group = screen.getByRole('group', { name: /migration actions/i });
  const button = (name: RegExp) => within(group).getByRole('button', { name });
  return { ...utils, server, migration, button };
}

describe('MigrationActions', () => {
  it('enables Finalize and Rollback for an approver on a completed migration', () => {
    const { button } = setup('web-01', 'approver');
    expect(button(/finalize/i)).not.toHaveAttribute('aria-disabled');
    expect(button(/roll back/i)).not.toHaveAttribute('aria-disabled');
    for (const name of [/approve/i, /^cut over/i, /sync/i, /retry/i, /cancel/i]) {
      expect(button(name)).toHaveAttribute('aria-disabled', 'true');
    }
  });

  it('keeps Finalize disabled for operators and explains why', () => {
    const { button } = setup('web-01', 'operator');
    expect(button(/finalize/i)).toHaveAttribute('aria-disabled', 'true');
    expect(button(/finalize/i)).toHaveAccessibleDescription(/approver role/i);
  });

  it('requires typing the VM name before finalizing', async () => {
    const user = userEvent.setup();
    const { button, server, migration } = setup('web-01', 'approver');

    await user.click(button(/finalize/i));
    const dialog = await screen.findByRole('alertdialog', { name: /finalize web-01/i });
    const confirm = within(dialog).getByRole('button', { name: /^finalize$/i });
    expect(confirm).toBeDisabled();

    const input = within(dialog).getByLabelText(/type web-01 to confirm/i);
    await user.type(input, 'web-0');
    expect(confirm).toBeDisabled();
    await user.type(input, '1');
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('finalized'));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  });

  it('asks for a reason before rolling back', async () => {
    const user = userEvent.setup();
    const { button, server, migration } = setup('legacy-rhel6-app', 'operator');

    expect(button(/cancel/i)).toHaveAttribute('aria-disabled', 'true');
    await user.click(button(/roll back/i));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    const confirm = within(dialog).getByRole('button', { name: /^roll back$/i });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/reason/i), 'kernel panic on RHOSO');
    await user.click(confirm);
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('rolling_back'));
  });

  it('lets an approver request the cutover of a converged warm migration', async () => {
    const user = userEvent.setup();
    const { button, server, migration } = setup('app-billing-01', 'approver');

    expect(screen.getByText(/next:/i)).toHaveTextContent(/cut over|approve/i);
    await user.click(button(/^cut over/i));
    const dialog = await screen.findByRole('alertdialog', { name: /cut over app-billing-01/i });
    expect(dialog).toHaveTextContent(/source VM will be stopped/i);
    await user.click(within(dialog).getByRole('button', { name: /start cutover/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
  });

  it('disables every action for viewers', () => {
    const { button } = setup('app-billing-01', 'viewer');
    for (const name of [/approve/i, /^cut over/i, /sync/i, /roll back/i, /retry/i, /cancel/i, /finalize/i]) {
      expect(button(name)).toHaveAttribute('aria-disabled', 'true');
    }
  });
});
