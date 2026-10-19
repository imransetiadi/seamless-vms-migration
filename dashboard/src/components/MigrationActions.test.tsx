import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import type { Migration, Role } from '../api/types';
import { forcedDowntime } from '../lib/forcedEstimate';
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
  it('says in the Roll back dialog what the rollback of a VMware migration deletes (SDD §16, review RV32A-06)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const done = server.migrations.find((x) => x.vm.name === 'web-01') as Migration;
    const migration: Migration = { ...done, strategy: 'vmware_warm', destination_server_id: done.destination_server_id ?? 'dst-9' };
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const approver: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={approver} />, { server, token: 'approver' });
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /roll back/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back web-01/i });
    expect(dialog.textContent).not.toMatch(/volumes are kept/i);
    expect(dialog.textContent).toMatch(/kit's volumes/i);
  });

  it('lists the actions available now first, the next step first, and the others under "Unavailable now" (SDD §16)', () => {
    const { button } = setup('web-01', 'approver');
    const group = screen.getByRole('group', { name: /migration actions/i });
    const names = within(group)
      .getAllByRole('button')
      .map((b) => b.textContent?.trim());
    expect(names.slice(0, 2)).toEqual(['Finalize', 'Roll back']);
    const label = within(group).getByText('Unavailable now');
    for (const name of [/approve/i, /^cut over/i, /sync/i, /retry/i, /cancel/i]) {
      const unavailable = button(name);
      // after the label, quieter and smaller, still focusable and saying why
      expect(label.compareDocumentPosition(unavailable) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(unavailable).toHaveAttribute('aria-disabled', 'true');
      expect(unavailable).toHaveAccessibleDescription(/.+/);
      expect(unavailable.className).toMatch(/min-h-8/);
    }
    // the next step's action is the primary one, at the 44 px size of primary actions
    expect(button(/finalize/i).className).toMatch(/bg-accent/);
    expect(button(/finalize/i).className).toMatch(/min-h-11/);
    expect(button(/roll back/i).className).toMatch(/min-h-11/);
  });

  it('puts every action of a viewer under "Unavailable now", each still saying why (SDD §16)', () => {
    setup('app-billing-01', 'viewer');
    const group = screen.getByRole('group', { name: /migration actions/i });
    // a viewer has no action: all of them are in the quiet row, under its label
    expect(within(group).getByText('Unavailable now')).toBeInTheDocument();
    expect(within(group).getAllByRole('button').every((b) => b.getAttribute('aria-disabled') === 'true')).toBe(true);
  });

  it('sets Cancel apart from other available actions, never from nothing', () => {
    const server = createTestServer();
    const render = (id: string) => {
      const m = server.migrations.find((x) => x.id === id) as Migration;
      const operator: Role = 'operator';
      const utils = renderWithApp(<MigrationActions migration={m} plan={server.plans.find((p) => p.id === m.plan_id) ?? null} role={operator} />, {
        server,
        token: 'operator',
      });
      const cancel = within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^cancel$/i });
      return { utils, cancel };
    };
    // in pre-copy an operator can only cancel: alone, Cancel is not pushed to the far side of the row
    const alone = render('mig-5d7e2b4a10');
    expect(alone.cancel).not.toHaveAttribute('aria-disabled');
    expect(alone.cancel.className).not.toMatch(/ml-auto/);
    alone.utils.unmount();
    // awaiting the cutover, Sync now comes first and Cancel keeps its distance from it
    const apart = render('mig-5d7e2b4a12');
    expect(apart.cancel.className).toMatch(/sm:ml-auto/);
  });

  it('never pulls the focus back to a button the user had left when a live move changes its row (SDD §16)', () => {
    const server = createTestServer();
    const done = server.migrations.find((m) => m.vm.name === 'web-01') as Migration;
    const plan = server.plans.find((p) => p.id === done.plan_id) ?? null;
    let move: (m: Migration) => void = () => {};
    function Moving() {
      const [m, setM] = useState(done);
      move = setM;
      const approver: Role = 'approver';
      return <MigrationActions migration={m} plan={plan} role={approver} />;
    }
    renderWithApp(<Moving />, { server, token: 'approver' });
    const finalize = within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^finalize$/i });
    finalize.focus();
    // the operator clicks the page background: the focus leaves the button for the page
    finalize.blur();
    expect(document.activeElement).toBe(document.body);
    // later a live event moves the migration and Finalize changes rows: the focus stays where the user left it
    act(() => move({ ...done, phase: 'finalized' }));
    expect(document.activeElement).toBe(document.body);
  });

  it('keeps the focus on a button that changes rows as the migration moves (SDD §16)', () => {
    const server = createTestServer();
    const done = server.migrations.find((m) => m.vm.name === 'web-01') as Migration;
    const plan = server.plans.find((p) => p.id === done.plan_id) ?? null;
    let move: (m: Migration) => void = () => {};
    function Moving() {
      const [m, setM] = useState(done);
      move = setM;
      const approver: Role = 'approver';
      return <MigrationActions migration={m} plan={plan} role={approver} />;
    }
    renderWithApp(<Moving />, { server, token: 'approver' });
    const group = screen.getByRole('group', { name: /migration actions/i });
    const finalize = within(group).getByRole('button', { name: /^finalize$/i });
    finalize.focus();
    expect(finalize).not.toHaveAttribute('aria-disabled');
    // finalized elsewhere: Finalize is no longer available and moves to the quiet row
    act(() => move({ ...done, phase: 'finalized' }));
    const moved = within(group).getByRole('button', { name: /^finalize$/i });
    expect(moved).toHaveAttribute('aria-disabled', 'true');
    expect(moved).toHaveFocus();
  });

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
    // maker-checker with no other approval: a request, not a start (SDD §16)
    const dialog = await screen.findByRole('alertdialog', { name: new RegExp(`request the cutover of ${migration.vm.name}`, 'i') });
    expect(dialog).toHaveTextContent(/starts once another approver approves it and pre-copy has converged/i);
    await user.click(within(dialog).getByRole('button', { name: /^request cutover$/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
    expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('precopy');
  });

  it.each([
    // [require_approval, require_distinct_approver, what the dialog says of an approval, whether the request records one]
    [false, true, null, false], // a plan made without approval keeps the distinct-approver default
    [false, false, 'This also records your approval', true],
    [true, false, 'This also records your approval', true],
    [true, true, 'Your request is not an approval', false],
  ] as const)(
    'claims an approval in the Cut over dialog only when the request records one (approval %s, distinct approver %s; SDD §5.4, §16)',
    async (requireApproval, distinct, says, records) => {
      const user = userEvent.setup({ delay: null });
      const server = createTestServer();
      const migration = server.migrations.find((x) => x.id === 'mig-9e4f6a8b53')!; // vm-5003, ready
      migration.approvals = [];
      const plan = server.plans.find((p) => p.id === migration.plan_id)!;
      plan.require_approval = requireApproval;
      plan.require_distinct_approver = distinct;
      const role: Role = 'approver';
      renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
      const group = screen.getByRole('group', { name: /migration actions/i });
      await user.click(within(group).getByRole('button', { name: /^cut over$/i }));
      const dialog = await screen.findByRole('alertdialog');
      for (const sentence of ['This also records your approval', 'Your request is not an approval']) {
        if (sentence === says) expect(dialog).toHaveTextContent(sentence);
        else expect(dialog).not.toHaveTextContent(sentence);
      }
      expect(dialog).toHaveTextContent(/the migration can be rolled back until it is finalized\./i);
      await user.click(within(dialog).getByRole('button', { name: /^(start|request) cutover$/i }));
      await waitFor(() => expect(server.migrations.find((x) => x.id === migration.id)?.cutover_requested).toBe(true));
      // what the dialog said is what the request recorded
      expect(server.migrations.find((x) => x.id === migration.id)!.approvals.some((a) => a.actor === 'sari')).toBe(records);
    },
  );

  it('claims an approval in the Force cutover dialog only when forcing records one (SDD §5.4, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('db-mysql-02', 'approver');
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;
    // a plan made without approval keeps the distinct-approver default: forcing records no approval
    plan.require_approval = false;
    plan.require_distinct_approver = true;
    await user.click(button(/^force cutover$/i));
    const dialog = await screen.findByRole('alertdialog', { name: 'Force the cutover of db-mysql-02?' });
    expect(dialog).not.toHaveTextContent('This also records your approval');
    expect(dialog).not.toHaveTextContent('Another approver still approves the cutover');
    expect(dialog).toHaveTextContent('The window and a free cutover slot still apply.');
  });

  it('counts the Force cutover reason as the API does: five emoji are five characters (SDD §5.4, audit AD-10)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('db-mysql-02', 'approver');
    await user.click(button(/^force cutover$/i));
    const dialog = await screen.findByRole('alertdialog', { name: 'Force the cutover of db-mysql-02?' });
    const confirm = within(dialog).getByRole('button', { name: /^force cutover$/i });
    const reason = within(dialog).getByRole('textbox', { name: /reason/i });
    await user.type(reason, '\u{1F600}'.repeat(5));
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    await user.type(reason, '\u{1F600}'.repeat(5));
    expect(confirm).not.toHaveAttribute('aria-disabled');
  });

  it('forces the cutover of a warm migration before it converged, with a reason (SDD §5.4, ADV-16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('db-mysql-02', 'approver');
    expect(migration).toMatchObject({ phase: 'syncing', strategy: 'warm' });
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;

    await user.click(button(/^force cutover$/i));
    const dialog = await screen.findByRole('alertdialog', { name: 'Force the cutover of db-mysql-02?' });
    expect(dialog).toHaveTextContent('The last pass (pass 2) changed 12.6 GiB.');
    // the figure a Force cutover records (SDD §5.4, §16): after the running pass - not the converged estimate;
    // forcing a VM whose downtime misses the SLO says so (db-mysql-02: about 21 min against 15)
    const forced = forcedDowntime(migration, plan);
    expect(Math.abs(forced - migration.estimate!.downtime_s)).toBeGreaterThan(60);
    expect(forced).toBeGreaterThan(plan.downtime_slo_s);
    expect(dialog).toHaveTextContent(`The estimated downtime is ${formatDuration(forced)}, over the SLO of ${formatDuration(plan.downtime_slo_s)}.`);
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
        estimated_downtime_s: forced, // the figure the dialog showed
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
    // the converged estimate is not what a Force cutover records: the dialog shows the downtime after the running full pass
    migration.estimate = { ...migration.estimate!, downtime_s: 600 };
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    await user.click(within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^force cutover$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: `Force the cutover of ${migration.vm.name}?` });
    expect(dialog).toHaveTextContent('The full copy is still running: the cutover follows it, and the final sync copies what changed meanwhile.');
    expect(plan?.downtime_slo_s).toBe(900);
    const forced = forcedDowntime(migration, plan!);
    expect(forced).toBeGreaterThan(900);
    expect(dialog).toHaveTextContent(`The estimated downtime is ${formatDuration(forced)}, over the SLO of 15m 00s.`);
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

  it('keeps Finalize unavailable while the source VM runs again, saying why, as the API refuses it (SDD §8.1)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'web-cache-01') as Migration;
    migration.source_running_at = '2026-10-08T11:00:00Z';
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    const finalize = within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^finalize$/i });
    expect(finalize).toHaveAttribute('aria-disabled', 'true');
    expect(finalize).toHaveAccessibleDescription('The source VM runs again: stop it, or roll it back, before finalizing.');
    await user.click(finalize);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('completed');
  });

  it('finalizes a migration whose finalize began deleting its source, now running again, by deleting it - the only finalize the API takes (SDD §8.1, audit OC-09)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'web-cache-01') as Migration;
    Object.assign(migration, { checkpoint: 'finalizing', source_running_at: '2026-10-08T11:00:00Z' });
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    await user.click(within(screen.getByRole('group', { name: /migration actions/i })).getByRole('button', { name: /^finalize$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /finalize web-cache-01/i });
    const box = within(dialog).getByRole('checkbox', { name: /also delete the source vm/i });
    expect(box).toBeChecked();
    expect(box).toBeDisabled();
    expect(box).toHaveAccessibleName(/runs again: only deleting it finalizes it/i);
    await user.type(within(dialog).getByLabelText(/type web-cache-01 to confirm/i), 'web-cache-01');
    await user.click(within(dialog).getByRole('button', { name: /^finalize$/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.phase).toBe('finalized'));
    expect(server.events.find((e) => e.migration_id === migration.id && e.data.action === 'finalize')?.data.delete_source).toBe(true);
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

  /** The dialog's field holds 2000 characters as the API counts them - 1500 emoji are 3000 units - and its confirm
   * refuses more, saying why (SDD §12, review RV50-06). */
  async function capsAsTheApi(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, label: RegExp, confirmName: RegExp) {
    const field = within(dialog).getByLabelText(label) as HTMLTextAreaElement;
    const confirm = within(dialog).getByRole('button', { name: confirmName });
    await user.click(field);
    await user.paste('\u{1F600}'.repeat(1500));
    expect([...field.value].length).toBe(1500);
    expect(confirm).not.toHaveAttribute('aria-disabled');
    await user.clear(field);
    await user.paste('a'.repeat(2001));
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('At most 2000 characters.');
  }

  it('caps a rollback reason at the API limit of 2000 characters (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('legacy-rhel6-app', 'operator');

    await user.click(button(/roll back/i));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    await capsAsTheApi(user, dialog, /reason/i, /^roll back$/i);
  });

  it('caps an approval comment and a cancel reason at 2000 characters (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('app-billing-01', 'approver');

    await user.click(button(/approve/i));
    const approve = await screen.findByRole('alertdialog', { name: /approve/i });
    await capsAsTheApi(user, approve, /comment/i, /^approve/i);
    await user.click(within(approve).getByRole('button', { name: /^cancel$/i }));
    await user.click(button(/^cancel/i));
    const cancel = await screen.findByRole('alertdialog', { name: /cancel/i });
    await capsAsTheApi(user, cancel, /reason/i, /^cancel migration$/i);
  });

  it('caps a cutover comment at 2000 characters, counted as the API counts them (SDD §12, review RV50-06)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('db-mysql-02', 'approver');
    await user.click(button(/^cut over/i));
    const dialog = await screen.findByRole('alertdialog', { name: /db-mysql-02/i });
    await capsAsTheApi(user, dialog, /comment/i, /cut ?over/i);
  });

  it("sends an approval comment, a rollback reason and a cancel reason as the API's Python strips them (SDD §12, review RV50-06)", async () => {
    const user = userEvent.setup({ delay: null });
    // a byte-order mark Python's strip keeps and JavaScript's trim takes; the spaces around go
    const approving = setup('app-billing-01', 'approver');
    await user.click(approving.button(/approve/i));
    const approve = await screen.findByRole('alertdialog', { name: /approve/i });
    await user.type(within(approve).getByLabelText(/comment/i), ' looks good\uFEFF ');
    await user.click(within(approve).getByRole('button', { name: /^approve/i }));
    await waitFor(() => expect(approving.server.migrations.find((m) => m.id === approving.migration.id)!.approvals.at(-1)?.comment).toBe('looks good\uFEFF'));
    approving.unmount();
    const rolling = setup('legacy-rhel6-app', 'operator');
    await user.click(rolling.button(/roll back/i));
    const rollback = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    await user.type(within(rollback).getByLabelText(/reason/i), ' undo it\uFEFF ');
    await user.click(within(rollback).getByRole('button', { name: /^roll back$/i }));
    await waitFor(() => expect(rolling.server.events.find((e) => e.migration_id === rolling.migration.id && e.data.action === 'rollback')?.data.reason).toBe('undo it\uFEFF'));
    rolling.unmount();
    const cancelling = setup('app-billing-01', 'operator');
    await user.click(cancelling.button(/^cancel/i));
    const cancel = await screen.findByRole('alertdialog', { name: /cancel/i });
    await user.type(within(cancel).getByLabelText(/reason/i), ' not now\uFEFF ');
    await user.click(within(cancel).getByRole('button', { name: /^cancel migration$/i }));
    await waitFor(() => expect(cancelling.server.events.find((e) => e.migration_id === cancelling.migration.id && e.data.action === 'cancel')?.data.reason).toBe('not now\uFEFF'));
  });

  it('sends the Force cutover reason it counted, a byte-order mark kept as Python keeps it (SDD §5.4, review RV50-06)', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('db-mysql-02', 'approver');
    await user.click(button(/^force cutover$/i));
    const dialog = await screen.findByRole('alertdialog', { name: 'Force the cutover of db-mysql-02?' });
    await user.type(within(dialog).getByRole('textbox', { name: /reason/i }), '\uFEFFabcdefghi');
    const confirm = within(dialog).getByRole('button', { name: /^force cutover$/i });
    expect(confirm).not.toHaveAttribute('aria-disabled');
    await user.click(confirm);
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.force_cutover).toMatchObject({ reason: '\uFEFFabcdefghi' }));
  });

  it("refuses a rollback reason the API's Python strips to nothing (SDD §12, review RV50-06)", async () => {
    const user = userEvent.setup({ delay: null });
    const { button } = setup('legacy-rhel6-app', 'operator');
    await user.click(button(/roll back/i));
    const dialog = await screen.findByRole('alertdialog', { name: /roll back legacy-rhel6-app/i });
    // U+0085 is whitespace to Python, not to JavaScript's trim
    await user.type(within(dialog).getByLabelText(/reason/i), '\u0085');
    const confirm = within(dialog).getByRole('button', { name: /^roll back$/i });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a reason first.');
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

  it("says a VMware cancel removes the recorded server and keeps the kit's volumes, named in the timeline (SDD §5.1, §7.2, §16, docs-drift DD3-02)", async () => {
    const user = userEvent.setup({ delay: null });
    const { button, migration } = setup('vm-erp-app-01', 'operator');
    expect([migration.strategy, migration.phase]).toEqual(['vmware_warm', 'syncing']);
    await user.click(button(/^cancel/i));
    const dialog = await screen.findByRole('alertdialog', { name: /cancel vm-erp-app-01/i });
    expect(dialog).toHaveTextContent(/the source VM keeps running/i);
    expect(dialog).toHaveTextContent(/the destination server it recorded is removed/i);
    expect(dialog).toHaveTextContent(/the migration kit's volumes stay, named in the timeline/i);
    expect(dialog).not.toHaveTextContent(/destination volumes and source snapshots/i);
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

  it("leads an advisor-held migration's next step with its recommendation, ahead of its own actions (SDD §16)", async () => {
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.id === 'mig-b2c4d6e871')!;
    expect(migration.advisor_hold).toBeTruthy();
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={'approver' as Role} />, { server, token: 'approver' });
    const group = screen.getByRole('group', { name: /migration actions/i });
    const link = within(group).getByRole('link', { name: 'See the recommendation' });
    expect(link).toHaveAttribute('href', `/advisor/recommendations/${migration.advisor_hold}`);
    expect(group.firstElementChild).toBe(link);
  });

  it('offers no recommendation link where no advisor decision waits', () => {
    setup('app-billing-01', 'approver');
    const group = screen.getByRole('group', { name: /migration actions/i });
    expect(within(group).queryByRole('link', { name: 'See the recommendation' })).toBeNull();
  });

  it('says in the Approve dialog that approving a requested cutover starts it and stops the source VM (SDD §5.4, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((x) => x.id === 'mig-9e4f6a8b53')!; // vm-fileserver-01, vmware_cold, ready
    Object.assign(migration, { approvals: [], cutover_requested: true, cutover_requested_by: 'rina', advisor_hold: null });
    const plan = server.plans.find((p) => p.id === migration.plan_id)!;
    Object.assign(plan, { require_approval: true, require_distinct_approver: true, auto_cutover: false, cutover_window: null });
    const role: Role = 'approver';
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={role} />, { server, token: role });
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /^approve$/i }));
    const dialog = await screen.findByRole('alertdialog', { name: `Approve the cutover of ${migration.vm.name}?` });
    expect(dialog).toHaveTextContent('Your approval lets the cutover start: the source VM will then be stopped and the downtime clock starts');
    expect(dialog).not.toHaveTextContent('still waits for');
    await user.click(within(dialog).getByRole('button', { name: 'Approve and cut over' }));
    await waitFor(() => expect(server.migrations.find((x) => x.id === migration.id)!.approvals.map((a) => a.actor)).toEqual(['sari']));
  });

  it('lets an approver request the cutover of a converged warm migration', async () => {
    const user = userEvent.setup({ delay: null });
    const { button, server, migration } = setup('app-billing-01', 'approver');

    expect(screen.getByText(/next:/i)).toHaveTextContent(/cut over|approve/i);
    await user.click(button(/^cut over/i));
    // its plan wants another approver and none approved: the dialog asks to request it (SDD §16)
    const dialog = await screen.findByRole('alertdialog', { name: /request the cutover of app-billing-01/i });
    expect(dialog).toHaveTextContent(/source VM will be stopped/i);
    await user.click(within(dialog).getByRole('button', { name: /request cutover/i }));
    await waitFor(() => expect(server.migrations.find((m) => m.id === migration.id)?.cutover_requested).toBe(true));
  });

  it('repeats in the Cut over dialog what the cutover will refuse, and still lets the cutover be requested (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const migration = server.migrations.find((m) => m.vm.name === 'app-billing-01') as Migration;
    const problem = "the NIC 00:50:56:00:00:01 of app-billing-01 on 'app-net': port port-old holds its MAC";
    migration.cutover_refusals = [problem];
    const plan = server.plans.find((p) => p.id === migration.plan_id) ?? null;
    renderWithApp(<MigrationActions migration={migration} plan={plan} role={"approver" as Role} />, { server, token: 'approver' });
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /^cut over/i }));
    const dialog = await screen.findByRole('alertdialog', { name: /request the cutover of app-billing-01/i });
    const refusals = within(dialog).getByRole('note', { name: 'Cutover will refuse' });
    expect(refusals).toHaveTextContent(problem);
    await user.click(within(dialog).getByRole('button', { name: /request cutover/i }));
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
    const dialog = await screen.findByRole('alertdialog', { name: /request the cutover of app-billing-01/i });
    const ignore = within(dialog).getByRole('checkbox', { name: /ignore the cutover window/i });
    expect(ignore).not.toBeChecked();
    await user.click(ignore);
    await user.click(within(dialog).getByRole('button', { name: /^request cutover$/i }));
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
