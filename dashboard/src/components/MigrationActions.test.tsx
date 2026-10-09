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
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('web-01', 'approver');

    await user.click(button(/finalize/i));
    const dialog = await screen.findByRole('alertdialog', { name: /finalize web-01/i });
    const confirm = within(dialog).getByRole('button', { name: /^finalize$/i });
    // unavailable until the name matches, but focusable and saying why (SDD §16)
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Type web-01 to confirm.');

    const input = within(dialog).getByLabelText(/type web-01 to confirm/i);
    await user.type(input, 'web-0');
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    await user.type(input, '1');
    expect(confirm).not.toHaveAttribute('aria-disabled');

    await user.click(confirm);
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('finalized'));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  });

  it('says a retry starts the migration over, not from a checkpoint (the API clears it)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('legacy-rhel6-app', 'operator');

    await user.click(button(/retry/i));
    const dialog = await screen.findByRole('alertdialog', { name: /retry legacy-rhel6-app/i });
    expect(dialog).not.toHaveTextContent(/checkpoint/i);
    expect(dialog).toHaveTextContent(/starts over/i);
    // this migration failed with its source stopped: the clock keeps running (SDD §5.2)
    expect(dialog).toHaveTextContent(/still stopped.*downtime clock keeps running/i);
  });

  it('caps a rollback reason at the API limit of 2000 characters (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('legacy-rhel6-app', 'operator');

    await user.click(button(/roll back/i));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    expect(within(dialog).getByLabelText(/reason/i)).toHaveAttribute('maxlength', '2000');
  });

  it('caps an approval comment and a cancel reason at 2000 characters (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('app-billing-01', 'approver');

    await user.click(button(/approve/i));
    const approve = await screen.findByRole('alertdialog', { name: /approve/i });
    expect(within(approve).getByLabelText(/comment/i)).toHaveAttribute('maxlength', '2000');
    await user.click(within(approve).getByRole('button', { name: /^cancel$/i }));
    await user.click(button(/^cancel/i));
    const cancel = await screen.findByRole('alertdialog', { name: /cancel/i });
    expect(within(cancel).getByLabelText(/reason/i)).toHaveAttribute('maxlength', '2000');
  });

  it('asks for a reason before rolling back', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('legacy-rhel6-app', 'operator');

    expect(button(/cancel/i)).toHaveAttribute('aria-disabled', 'true');
    await user.click(button(/roll back/i));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    const confirm = within(dialog).getByRole('button', { name: /^roll back$/i });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a reason first.');
    await user.type(within(dialog).getByLabelText(/reason/i), 'kernel panic on RHOSO');
    await user.click(confirm);
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('rolling_back'));
  });

  it('lets an approver request the cutover of a converged warm migration', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('app-billing-01', 'approver');

    expect(screen.getByText(/next:/i)).toHaveTextContent(/cut over|approve/i);
    await user.click(button(/^cut over/i));
    const dialog = await screen.findByRole('alertdialog', { name: /cut over app-billing-01/i });
    expect(dialog).toHaveTextContent(/source VM will be stopped/i);
    await user.click(within(dialog).getByRole('button', { name: /start cutover/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
  });

  it('offers to ignore a closed window with the first cutover request (SDD §5.4)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;
    migration.cutover_requested = false;
    migration.force_window = false;
    plan.cutover_window = { start: new Date(Date.now() + 86_400_000).toISOString(), end: new Date(Date.now() + 90_000_000).toISOString() };
    const approver = 'approver' as const; // the component's role prop, not an ARIA role
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={approver} />, { server, token: approver });
    const group = screen.getByRole('group', { name: /migration actions/i });

    await user.click(within(group).getByRole('button', { name: /^cut over$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /cut over app-billing-01 now/i });
    const ignore = within(dialog).getByRole('checkbox', { name: /ignore the cutover window/i });
    expect(ignore).not.toBeChecked();
    await user.click(ignore);
    await user.click(within(dialog).getByRole('button', { name: /^start cutover$/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
    expect(server.migrations.find((m) => m.id === migration.id)?.force_window).toBe(true);
  });

  it('lets an approver let a requested cutover start outside a closed window (SDD §5.4)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;
    migration.cutover_requested = true;
    migration.force_window = false;
    plan.cutover_window = { start: new Date(Date.now() + 86_400_000).toISOString(), end: new Date(Date.now() + 90_000_000).toISOString() };
    const approver = 'approver' as const; // the component's role prop, not an ARIA role
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={approver} />, { server, token: approver });
    const group = screen.getByRole('group', { name: /migration actions/i });

    await user.click(within(group).getByRole('button', { name: /^cut over outside the window$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /let app-billing-01 cut over outside the window/i });
    expect(dialog).toHaveTextContent(/waits for the cutover window/i);
    // forcing the window is not an immediate start: the rest of the gate still applies
    expect(dialog).toHaveTextContent(/as soon as the plan, its wave and a free cutover slot allow/i);
    expect(dialog).not.toHaveTextContent(/ignore the cutover window/i);
    await user.click(within(dialog).getByRole('button', { name: /^cut over outside the window$/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.force_window).toBe(true));
  });

  it('disables every action for viewers', () => {
    const { button } = setup('app-billing-01', 'viewer');
    for (const name of [/approve/i, /^cut over/i, /sync/i, /roll back/i, /retry/i, /cancel/i, /finalize/i]) {
      expect(button(name)).toHaveAttribute('aria-disabled', 'true');
    }
  });
});
