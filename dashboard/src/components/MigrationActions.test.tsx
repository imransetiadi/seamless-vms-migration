import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Migration, Role } from '../api/types';
import { formatDuration } from '../lib/format';
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

  it('requests a warm cutover during pre-copy, which starts once it converges (SDD §5.4, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.id === 'mig-5d7e2b4a10')!;
    expect(migration).toMatchObject({ phase: 'precopy', strategy: 'warm' });
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /^cut over$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: new RegExp(`cut over ${migration.vm.name} once pre-copy converges`, 'i') });
    expect(dialog).toHaveTextContent(/starts once pre-copy has converged/i);
    await user.click(within(dialog).getByRole('button', { name: /^request cutover$/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
    expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('precopy');
  });

  it('forces the cutover of a warm migration before it converged, with a reason (SDD §5.4, ADV-16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('db-mysql-02', 'approver');
    expect(migration).toMatchObject({ phase: 'syncing', strategy: 'warm' });
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;

    await user.click(button(/^force cutover$/i));
    const dialog = await screen.findByRole('alertdialog', { name: 'Force the cutover of db-mysql-02?' });
    expect(dialog).toHaveTextContent('The last pass (pass 2) changed 12.6 GiB.');
    // forcing a VM whose estimate misses the SLO says so (db-mysql-02: about 24 min against 15)
    expect(migration.estimate!.downtime_s).toBeGreaterThan(plan.downtime_slo_s);
    expect(dialog).toHaveTextContent(`The estimated downtime is ${formatDuration(migration.estimate!.downtime_s)}, over the SLO of ${formatDuration(plan.downtime_slo_s)}.`);
    // maker-checker still applies (SDD §5.4, ADV-17)
    expect(dialog).toHaveTextContent('Another approver still approves the cutover');
    const confirm = within(dialog).getByRole('button', { name: /^force cutover$/i });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a reason of at least 10 characters.');
    const reason = within(dialog).getByRole('textbox', { name: /reason/i });
    await user.type(reason, 'Too short');
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    await user.clear(reason);
    await user.type(reason, 'The change window closes in 20 minutes');
    expect(confirm).not.toHaveAttribute('aria-disabled');

    await user.click(confirm);
    await waitFor(() =>
      expect(server.migrations.find((m) => m.id === migration.id)?.force_cutover).toMatchObject({
        actor: 'sari',
        reason: 'The change window closes in 20 minutes',
        delta_bytes: Math.round(12.6 * 2 ** 30),
      }),
    );
    expect(server.events.at(-1)).toMatchObject({ kind: 'migration.force_cutover', actor: 'sari' });
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  });

  it('says the full copy is still running when a pre-copy is forced', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.id === 'mig-5d7e2b4a10')!;
    expect(migration).toMatchObject({ phase: 'precopy', sync_passes: [] });
    // an estimate within the SLO
    migration.estimate = { ...migration.estimate!, downtime_s: 600 };
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    await user.click(within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^force cutover$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: `Force the cutover of ${migration.vm.name}?` });
    expect(dialog).toHaveTextContent('The full copy is still running: the cutover follows it, and the final sync copies what changed meanwhile.');
    expect(plan?.downtime_slo_s).toBe(900);
    expect(dialog).toHaveTextContent('The estimated downtime is 10m 00s against an SLO of 15m 00s.');
    expect(dialog).not.toHaveTextContent(/last pass/i);
  });

  it('offers Force cutover only for warm migrations, and says why it is unavailable once they converged (SDD §5.4)', () => {
    const server = createTestServer();
    const cold = server.migrations.find((m) => m.id === 'mig-5d7e2b4a13')!;
    expect(cold.strategy).toBe('cold');
    const role: Role = 'approver';
    const { unmount } = renderWithApp(<MigrationActions migration={cold} plan={null} role={role} />, { server, token: role });
    expect(within(screen.getByRole('group', { name: /migration actions/i })).queryByRole('button', { name: /force cutover/i })).not.toBeInTheDocument();
    unmount();

    const { button } = setup('web-01', 'approver');
    expect(button(/^force cutover$/i)).toHaveAttribute('aria-disabled', 'true');
    expect(button(/^force cutover$/i)).toHaveAccessibleDescription('A Force cutover skips convergence: it applies while pre-copy or delta passes run.');
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

  it('warns in Finalize while the source VM runs again (SDD §8.1, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'web-cache-01') as Migration;
    migration.source_running_at = '2026-10-08T11:00:00Z';
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    await user.click(within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /finalize/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /finalize web-cache-01/i });
    expect(dialog).toHaveTextContent('The source VM runs again: what it wrote since is not on RHOSO. Stop it, or roll it back instead.');
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
    expect(dialog).not.toHaveTextContent(/reopens/i);
  });

  it('says a retry reopens a completed plan, which then runs again (SDD §5.1)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.phase === 'rolled_back') as Migration;
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;
    plan.status = 'completed';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={'operator' as Role} />, { server, token: 'operator' });

    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /retry/i }));
    const dialog = await screen.findByRole('alertdialog', { name: new RegExp(`retry ${migration.vm.name}`, 'i') });
    expect(dialog).toHaveTextContent(/the plan has completed: the retry reopens it, so the new attempt runs/i);
    await user.click(within(dialog).getByRole('button', { name: /^retry$/i }));
    await waitFor(() => expect(plan.status).toBe('running'));
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

  it('says a cancel removes the data copied so far when the migration has a data path (SDD §5.1, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    // converged with recorded passes, or copying its first pass: the cancel cleans up after it
    for (const vm of ['app-billing-01', 'db-pg-01']) {
      const { button, migration, unmount } = setup(vm, 'operator');
      expect(migration.sync_passes.length > 0 || migration.phase === 'precopy').toBe(true);
      await user.click(button(/^cancel/i));
      const dialog = await screen.findByRole('alertdialog', { name: new RegExp(`cancel ${vm}`, 'i') });
      expect(dialog).toHaveTextContent(/the source VM keeps running/i);
      expect(dialog).toHaveTextContent(/the data copied so far — destination volumes and source snapshots — is then removed/i);
      expect(dialog).not.toHaveTextContent(/not touched/i);
      unmount();
    }
    // nothing copied yet: nothing to remove
    const { button, migration } = setup('ad-dc-01', 'operator');
    expect(migration.phase).toBe('ready');
    expect(migration.sync_passes).toHaveLength(0);
    await user.click(button(/^cancel/i));
    const dialog = await screen.findByRole('alertdialog', { name: /cancel ad-dc-01/i });
    expect(dialog).toHaveTextContent(/the source VM is not touched/i);
    expect(dialog).not.toHaveTextContent(/data copied so far/i);
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
