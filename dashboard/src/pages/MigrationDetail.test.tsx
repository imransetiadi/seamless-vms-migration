import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { queryKeys } from '../api/hooks';
import type { Migration } from '../api/types';
import { STRATEGY_LABELS } from '../lib/status';
import { createTestClient, createTestServer, renderWithApp } from '../test/utils';
import MigrationDetail from './MigrationDetail';

function renderMigration(id: string) {
  return renderWithApp(<MigrationDetail />, { route: `/migrations/${id}`, path: '/migrations/:migrationId', token: 'viewer' });
}

describe('MigrationDetail', () => {
  it('says when a requested cutover may start outside the cutover window (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    // an approval too, so the request shows in the list of approvals
    m.approvals = [{ actor: 'approver-1', at: '2026-10-08T11:50:00Z', comment: 'go after the backup' }];
    m.cutover_requested = true;
    m.force_window = true;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    expect(await screen.findByText('Cutover requested')).toBeInTheDocument();
    expect(screen.getByText('An approver allowed it to start outside the cutover window.')).toBeInTheDocument();
    expect(screen.getByText('Approved by approver-1')).toBeInTheDocument();
  });

  it('names the strategy of each earlier attempt beside the attempts count (SDD §4.2, §14.4)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    m.attempts = 2;
    m.attempt_strategies = ['warm', 'cold'];
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const term = await screen.findByText('Attempts');
    const value = term.nextElementSibling as HTMLElement;
    expect(value).toHaveTextContent('2');
    expect(value).toHaveTextContent(`earlier: ${STRATEGY_LABELS.warm}, then ${STRATEGY_LABELS.cold}`);
  });

  it('shows the attempts count alone when nothing was retried', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    m.attempts = 0;
    m.attempt_strategies = [];
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const term = await screen.findByText('Attempts');
    expect((term.nextElementSibling as HTMLElement).textContent).toBe('0');
  });

  it('shows a cutover request in a plan that needs no approval (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    server.plans.find((p) => p.id === m.plan_id)!.require_approval = false;
    m.approvals = [];
    m.cutover_requested = true;
    m.force_window = false;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    expect(await screen.findByText('This plan does not require a cutover approval.')).toBeInTheDocument();
    expect(screen.getByText('Cutover requested')).toBeInTheDocument();
    expect(screen.queryByText(/outside the cutover window/i)).not.toBeInTheDocument();
  });

  it('names the guest OS and the verification profile a Windows guest gets (SDD §7.5, §9.5)', async () => {
    renderMigration('mig-5d7e2b4a13');
    const vm = await screen.findByRole('region', { name: /^vm$/i });
    const os = within(vm).getByRole('definition', { name: /guest os/i });
    expect(os).toHaveTextContent('Windows Server 2019');
    await waitFor(() => expect(os).toHaveTextContent(/verified on tcp 3389, without the console check/i));
  });

  it('does not say pre-flight passed before it ran (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'ready')!;
    m.findings = [];
    m.phase = 'pending';
    m.phase_history = [{ from_phase: null, to_phase: 'pending', at: '2026-10-08T11:00:00Z', reason: 'migration created', actor: 'sari' }];
    const { unmount } = renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    let findings = await screen.findByRole('region', { name: /^findings$/i });
    expect(findings).toHaveTextContent(/no findings yet — pre-flight runs when the plan is validated/i);
    unmount();

    m.phase = 'validating';
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    findings = await screen.findByRole('region', { name: /^findings$/i });
    expect(findings).toHaveTextContent(/no findings yet — pre-flight is running/i);
    expect(findings).not.toHaveTextContent(/pre-flight passed/i);
  });

  it('says on the convergence chart how many passes a long wait dropped (SDD §5.4, §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'awaiting_cutover' && x.strategy === 'warm');
    const plan = server.plans.find((p) => p.id === m?.plan_id);
    if (!m || !plan) throw new Error('fixture: a warm migration awaiting cutover');
    const delta = m.sync_passes.at(-1)!;
    const kept = Array.from({ length: plan.max_sync_passes - 1 }, (_, i) => ({ ...delta, number: i + 2 }));
    const latest = Array.from({ length: 20 }, (_, i) => ({ ...delta, number: plan.max_sync_passes + 31 + i }));
    m.sync_passes = [...m.sync_passes.slice(0, 1), ...kept, ...latest];
    m.sync_bytes_dropped = 30 * 2 ** 30;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const chart = await screen.findByRole('region', { name: /sync-pass convergence/i });
    await waitFor(() =>
      expect(chart).toHaveTextContent(
        `Passes ${plan.max_sync_passes + 1}–${plan.max_sync_passes + 30} are no longer listed: the history keeps the first ${plan.max_sync_passes} passes and the latest 20. The 30.0 GiB they transferred still counts toward the total.`,
      ),
    );
  });

  it('shows the transferred bytes and the disk used bytes as two figures, never one over the other (SDD §4.2, §16)', async () => {
    const GiB = 2 ** 30;
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'awaiting_cutover' && x.strategy === 'warm');
    if (!m) throw new Error('fixture: a warm migration awaiting cutover');
    // every delta pass adds: a warm migration transfers more than its disk holds
    m.bytes_total = 110 * GiB;
    m.bytes_transferred = 112 * GiB;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const progress = await screen.findByRole('region', { name: /^progress$/i });
    expect(within(progress).getByRole('definition', { name: /^transferred$/i })).toHaveTextContent(/^112 GiB$/);
    expect(within(progress).getByRole('definition', { name: /^disk used$/i })).toHaveTextContent(/^110 GiB$/);
    expect(progress).not.toHaveTextContent('112 GiB / 110 GiB');
  });

  it('names the running step from the phase when the API lists only passes that ended (SDD §4.2, §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'syncing' && x.strategy === 'warm');
    if (!m) throw new Error('fixture: a warm migration syncing');
    m.sync_passes = m.sync_passes.filter((p) => p.ended_at !== null);
    const next = Math.max(...m.sync_passes.map((p) => p.number)) + 1;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const progress = await screen.findByRole('region', { name: /^progress$/i });
    expect(within(progress).getByRole('definition', { name: /^current step$/i })).toHaveTextContent(`#${next} delta`);
    const chart = screen.getByRole('region', { name: /sync-pass convergence/i });
    expect(chart).toHaveTextContent(`Pass ${next} (delta) is running.`);
  });

  it('keeps Cutover unavailable while its plan loads: the window and approval policy are not known yet (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'awaiting_cutover' && x.strategy === 'warm');
    if (!m) throw new Error('fixture: a warm migration awaiting cutover');
    m.cutover_requested = false;
    // the plan's answer waits until the test lets it through
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    const wrapFetch = (fetchImpl: typeof fetch): typeof fetch => async (input, init) => {
      if (String(input).endsWith(`/plans/${m.plan_id}`)) await held;
      return fetchImpl(input, init);
    };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'approver', server, wrapFetch });
    const cutover = await screen.findByRole('button', { name: /^cut over$/i });
    expect(cutover).toHaveAttribute('aria-disabled', 'true');
    expect(cutover).toHaveAccessibleDescription(/the plan is loading/i);
    release();
    await waitFor(() => expect(screen.getByRole('button', { name: /^cut over$/i })).not.toHaveAttribute('aria-disabled'));
  });

  it('says when its plan cannot be loaded and keeps Cutover unavailable until it can (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.phase === 'awaiting_cutover' && x.strategy === 'warm');
    if (!m) throw new Error('fixture: a warm migration awaiting cutover');
    m.cutover_requested = false;
    const index = server.plans.findIndex((p) => p.id === m.plan_id);
    const [plan] = server.plans.splice(index, 1);
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'approver', server });
    expect(await screen.findByRole('alert')).toHaveTextContent(/the plan of this migration is unavailable/i);
    const cutover = screen.getByRole('button', { name: /^cut over$/i });
    expect(cutover).toHaveAttribute('aria-disabled', 'true');
    expect(cutover).toHaveAccessibleDescription(/the plan could not be loaded/i);

    // once the plan loads again, Cutover is available
    server.plans.splice(index, 0, plan!);
    await user.click(within(screen.getByRole('alert')).getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /^cut over$/i })).not.toHaveAttribute('aria-disabled'));
    expect(screen.queryByText(/the plan of this migration is unavailable/i)).not.toBeInTheDocument();
  });

  it('warns that the source VM is still stopped after a retried cutover (SDD §5.2)', async () => {
    const server = createTestServer();
    const retried = server.migrations.find((m) => m.id === 'mig-e5f7a9b1a0');
    if (!retried?.downtime_started_at || retried.downtime_ended_at) throw new Error('fixture: failed with the source stopped');
    retried.phase = 'ready';
    retried.error = null;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${retried.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/source VM is still stopped/i);
    expect(alert).toHaveTextContent(/downtime clock keeps running/i);
  });

  it('asks before a strategy change clears the approvals and the cutover request (SDD §5.4, §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.estimates.filter((e) => e.eligible).length >= 2)!;
    m.phase = 'ready';
    m.approvals = [{ actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null }];
    m.cutover_requested = true;
    const other = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: `Use ${STRATEGY_LABELS[other]}` }));
    const dialog = await screen.findByRole('alertdialog', { name: /change the strategy/i });
    expect(dialog).toHaveTextContent(/clears 1 approval and the cutover request/i);
    await user.click(within(dialog).getByRole('button', { name: /^change and clear$/i }));
    await waitFor(() => expect(m.strategy).toBe(other));
    expect(m.approvals).toEqual([]);
    expect(m.cutover_requested).toBe(false);
  });

  it('counts the approvals on the server when another strategy is chosen, not the migration loaded before them (SDD §5.4)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.estimates.filter((e) => e.eligible).length >= 2)!;
    m.phase = 'ready';
    m.approvals = [];
    m.cutover_requested = false;
    const other = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const use = await screen.findByRole('button', { name: `Use ${STRATEGY_LABELS[other]}` });
    // the page shows the migration without approvals; then an approver approves elsewhere
    expect(await screen.findByText(/no cutover approvals yet|does not require a cutover approval/i)).toBeInTheDocument();
    m.approvals = [{ actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null }];

    await user.click(use);
    const dialog = await screen.findByRole('alertdialog', { name: /change the strategy/i });
    expect(dialog).toHaveTextContent(/clears 1 approval/i);
    expect(m.strategy).not.toBe(other);
  });

  it('changes the strategy at once when nothing would be cleared', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.estimates.filter((e) => e.eligible).length >= 2)!;
    m.phase = 'ready';
    m.approvals = [];
    m.cutover_requested = false;
    const other = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: `Use ${STRATEGY_LABELS[other]}` }));
    await waitFor(() => expect(m.strategy).toBe(other));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('announces a strategy change from a live region already on the page (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.estimates.filter((e) => e.eligible).length >= 2)!;
    m.phase = 'ready';
    m.approvals = [];
    m.cutover_requested = false;
    const other = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const use = await screen.findByRole('button', { name: `Use ${STRATEGY_LABELS[other]}` });
    const regions = screen.getAllByRole('status');

    await user.click(use);
    await waitFor(() => expect(m.strategy).toBe(other));
    const announced = await screen.findByText(`${m.vm.name}: strategy set to ${STRATEGY_LABELS[other]}.`);
    // a live region inserted together with its text is not reliably read
    expect(regions).toContain(announced.closest('[role="status"]'));
  });

  it("lists the migration's recommendations: what was recommended and what was decided (SDD §16, ADV-01)", async () => {
    renderMigration('mig-5d7e2b4a10');
    const advisor = await screen.findByRole('region', { name: /^advisor$/i });
    const decision = await within(advisor).findByText('Decision: Warm migration — applied automatically by policy global-default (67% ≥ 60%)');
    expect(decision.closest('li')).toHaveTextContent('Recommended: Warm migration · 67% confidence');
    // the entries are this migration's: no link to itself
    expect(within(advisor).queryByRole('link', { name: 'db-pg-01' })).not.toBeInTheDocument();
    const estimates = screen.getByRole('region', { name: /^estimates$/i });
    expect(within(estimates).getByRole('rowheader', { name: /warm/i })).toHaveTextContent(/in use/i);
    expect(document.body).not.toHaveTextContent(/selected/i);
  });

  it('says a migration with no advisor output has none, in one place', async () => {
    renderMigration('mig-7a9b1c3d41');
    const advisor = await screen.findByRole('region', { name: /^advisor$/i });
    expect(await within(advisor).findByText('No advisor recommendations or notes for this migration.')).toBeInTheDocument();
  });

  it('shows no advisor wait on a cancelled migration, whatever hold it stored (SDD §14.6)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-b2c4d6e871')!;
    m.phase = 'cancelled'; // a hold stored before a cancel cleared it
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    expect(await screen.findByRole('heading', { level: 1, name: m.vm.name })).toBeInTheDocument();
    expect(screen.queryByText(/^Waiting for advisor approval\./)).not.toBeInTheDocument();
  });

  it('shows that the migration waits for advisor approval, to everyone, and the actions only to advisor.approve (SDD §14.6, ADV-04)', async () => {
    const { server } = renderMigration('mig-b2c4d6e871');
    const hold = await screen.findByText(/^Waiting for advisor approval\./);
    const recId = server.migrations.find((m) => m.id === 'mig-b2c4d6e871')!.advisor_hold!;
    expect(within(hold.closest('[role="status"]') as HTMLElement).getByRole('link', { name: 'See the recommendation' })).toHaveAttribute(
      'href',
      `/advisor/recommendations/${recId}`,
    );
    const advisor = await screen.findByRole('region', { name: /^advisor$/i });
    await within(advisor).findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    expect(within(advisor).queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
  });

  it('overrides the decision with the reason the policy asks for, then runs the chosen option (SDD §14.6, ADV-04)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<MigrationDetail />, { route: '/migrations/mig-b2c4d6e871', path: '/migrations/:migrationId', token: 'admin' });
    const advisor = await screen.findByRole('region', { name: /^advisor$/i });
    const group = await within(advisor).findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    const dialog = await screen.findByRole('alertdialog', { name: 'Override the recommendation for analytics-node-2?' });
    // another eligible option: running the recommended one is approving it
    expect(within(within(dialog).getByLabelText('Run instead')).getAllByRole('option').map((o) => o.textContent)).toEqual(['Cold migration', 'Warm migration']);
    await user.selectOptions(within(dialog).getByLabelText('Run instead'), 'cold');
    const reason = within(dialog).getByLabelText(/^Reason/);
    // the Finance policy asks for a reason of at least 10 characters
    expect(reason).toBeRequired();
    await user.type(reason, 'too short');
    const confirm = within(dialog).getByRole('button', { name: 'Override' });
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('Enter a reason of at least 10 characters.');
    await user.click(confirm);
    expect(server.migrations.find((m) => m.id === 'mig-b2c4d6e871')!.advisor_hold).not.toBeNull();
    await user.type(reason, ': the window fits a cold cutover');
    await user.click(confirm);
    expect(await within(advisor).findByText('Decision: Cold migration — overridden by rina')).toBeInTheDocument();
    expect(within(advisor).getByText('too short: the window fits a cold cutover')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/^Waiting for advisor approval\./)).not.toBeInTheDocument());
    expect(server.migrations.find((m) => m.id === 'mig-b2c4d6e871')).toMatchObject({ strategy: 'cold', advisor_hold: null });
    expect(within(advisor).getByText('Overridden: analytics-node-2 runs Cold migration.')).toBeInTheDocument();
  });

  it('asks for no reason when the policy in force does not (SDD §14.6)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    server.policies[0]!.require_override_reason = false;
    renderWithApp(<MigrationDetail />, { route: '/migrations/mig-b2c4d6e871', path: '/migrations/:migrationId', token: 'admin', server });
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    const dialog = await screen.findByRole('alertdialog', { name: 'Override the recommendation for analytics-node-2?' });
    expect(within(dialog).getByLabelText('Reason (optional)')).not.toBeRequired();
    await user.selectOptions(within(dialog).getByLabelText('Run instead'), 'cold');
    await user.click(within(dialog).getByRole('button', { name: 'Override' }));
    expect(await screen.findByText('Decision: Cold migration — overridden by rina')).toBeInTheDocument();
  });

  it('lists the cutover request and the approvals in time order, and says when an approval does not count (SDD §16, ADV-17)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!; // web-01: requested by rina, approved by sari
    m.approvals = [...m.approvals, { actor: 'rina', at: '2026-10-08T11:59:00Z', comment: 'Approving my own request too' }];
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const panel = await screen.findByRole('region', { name: /^approvals$/i });
    const items = within(within(panel).getByRole('list')).getAllByRole('listitem');
    expect(items.map((li) => li.querySelector('strong')?.textContent)).toEqual(['Requested by rina', 'Approved by sari', 'Approved by rina']);
    expect(items[1]).toHaveTextContent('Pilot approved in CAB-2291');
    expect(items[2]).toHaveTextContent('Does not count: rina requested the cutover.');
    expect(await within(panel).findByText(/an approver other than the requester/i)).toBeInTheDocument();
  });

  it('says an approval does not count because its approver forced the cutover (SDD §5.4, §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!; // web-01: requested by rina, approved by sari
    // sari, who approved, then forced the cutover: her approval no longer counts, and not because she requested it
    m.force_cutover = { actor: 'sari', at: '2026-10-08T11:59:30Z', reason: 'the delta never converges on this VM', delta_bytes: 0, estimated_downtime_s: 600 };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const panel = await screen.findByRole('region', { name: /^approvals$/i });
    const items = within(within(panel).getByRole('list')).getAllByRole('listitem');
    const sari = items.find((li) => li.querySelector('strong')?.textContent === 'Approved by sari')!;
    expect(sari).toHaveTextContent('Does not count: sari forced the cutover.');
  });

  it("keeps Approve from the cutover's requester, with the reason, and lets another approver approve (SDD §5.4, ADV-17)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-9e4f6a8b53')!; // vm-5003, ready
    m.approvals = [];
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    expect([plan.require_approval, plan.require_distinct_approver]).toEqual([true, true]);
    const view = renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'approver', server });
    const group = await screen.findByRole('group', { name: /migration actions/i });
    // Cut over waits for the plan: its window and approval policy decide what the dialog says (SDD §16)
    await waitFor(() => expect(within(group).getByRole('button', { name: /^cut over/i })).not.toHaveAttribute('aria-disabled'));
    await user.click(within(group).getByRole('button', { name: /^cut over/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveTextContent('Your request is not an approval: another approver approves the cutover.');
    await user.click(within(dialog).getByRole('button', { name: /start cutover|request cutover/i }));
    await waitFor(() => expect(within(group).getByRole('button', { name: /^approve$/i })).toHaveAttribute('aria-disabled', 'true'));
    expect(within(group).getByRole('button', { name: /^approve$/i })).toHaveAccessibleDescription('You requested this cutover. Another approver must approve it.');
    expect(m).toMatchObject({ cutover_requested_by: 'sari', approvals: [] });
    view.unmount();
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const again = await screen.findByRole('group', { name: /migration actions/i });
    expect(within(again).getByRole('button', { name: /^approve$/i })).not.toHaveAttribute('aria-disabled');
  });

  it("does not offer another strategy while the advisor's decision waits for approval, and says why (SDD §12, §14.6, §16)", async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-b2c4d6e871')!;
    expect(m.advisor_hold).toBeTruthy();
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const estimates = await screen.findByRole('table', { name: /estimates per strategy/i });
    const others = within(estimates)
      .getAllByRole('button', { name: /^use /i })
      .filter((b) => !b.getAttribute('aria-label')?.toLowerCase().includes('handover'));
    expect(others.length).toBeGreaterThan(0);
    for (const button of others) {
      expect(button).toHaveAttribute('aria-disabled', 'true');
      expect(button).toHaveAccessibleDescription(/advisor's decision waits for approval/i);
    }
  });

  it("keeps Approve and Override from the decision's requester, with the reason (SDD §14.6, ADV-17)", async () => {
    const server = createTestServer();
    const db = server.migrations.find((x) => x.vm.name === 'analytics-node-2')!;
    const rec = server.recommendations.find((r) => r.id === db.advisor_hold)!;
    rec.requested_by = 'rina'; // the admin persona validated the plan
    renderWithApp(<MigrationDetail />, { route: `/migrations/${db.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    for (const name of ['Approve', 'Override']) {
      const button = within(group).getByRole('button', { name });
      expect(button).toHaveAttribute('aria-disabled', 'true');
      expect(button).toHaveAccessibleDescription('You requested this decision. Another approver must decide it.');
    }
  });

  it('holds a decision to the rules its policy recorded, even after the policy was relaxed (SDD §14.6)', async () => {
    const server = createTestServer();
    const db = server.migrations.find((x) => x.vm.name === 'analytics-node-2')!;
    const rec = server.recommendations.find((r) => r.id === db.advisor_hold)!;
    rec.requested_by = 'rina'; // the admin persona validated the plan
    rec.decision = { ...rec.decision, require_distinct_approver: true, require_override_reason: true };
    for (const p of server.policies) Object.assign(p, { require_distinct_approver: false, require_override_reason: false });
    const { queryClient } = renderWithApp(<MigrationDetail />, { route: `/migrations/${db.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    // the relaxed policies have loaded: what shows now is what they would allow
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    for (const name of ['Approve', 'Override']) {
      const button = within(group).getByRole('button', { name });
      expect(button).toHaveAttribute('aria-disabled', 'true');
      expect(button).toHaveAccessibleDescription('You requested this decision. Another approver must decide it.');
    }
  });

  it('asks for the override reason the decision\'s policy required, even after the policy was relaxed (SDD §14.6)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const db = server.migrations.find((x) => x.vm.name === 'analytics-node-2')!;
    const rec = server.recommendations.find((r) => r.id === db.advisor_hold)!;
    rec.requested_by = 'bayu'; // another user validated the plan
    rec.decision = { ...rec.decision, require_distinct_approver: true, require_override_reason: true };
    for (const p of server.policies) Object.assign(p, { require_distinct_approver: false, require_override_reason: false });
    const { queryClient } = renderWithApp(<MigrationDetail />, { route: `/migrations/${db.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByLabelText(/^Reason/)).toBeRequired();
    expect(within(dialog).queryByLabelText('Reason (optional)')).not.toBeInTheDocument();
  });

  it("keeps Approve and Override while the plan sets the VM's strategy, and Approve from an approved decision, with the reason (SDD §14.6)", async () => {
    const server = createTestServer();
    const db = server.migrations.find((x) => x.vm.name === 'analytics-node-2')!;
    const plan = server.plans.find((p) => p.id === db.plan_id)!;
    const overrides = plan.strategy_overrides;
    plan.strategy_overrides = { ...overrides, [db.vm.source_id]: 'cold' };
    const view = renderWithApp(<MigrationDetail />, { route: `/migrations/${db.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    for (const name of ['Approve', 'Override']) {
      const button = within(group).getByRole('button', { name });
      await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'));
      expect(button).toHaveAccessibleDescription('The plan sets the strategy of analytics-node-2 (Cold migration): change it in the plan.');
    }
    view.unmount();
    plan.strategy_overrides = overrides;
    const rec = server.recommendations.find((r) => r.id === db.advisor_hold)!;
    rec.decision = { ...rec.decision, status: 'approved', final_option: rec.recommendation.option };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${db.id}`, path: '/migrations/:migrationId', token: 'admin', server });
    const approved = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    const approve = within(approved).getByRole('button', { name: 'Approve' });
    expect(approve).toHaveAttribute('aria-disabled', 'true');
    expect(approve).toHaveAccessibleDescription('The decision is already approved.');
    // another option is still a person's call while nothing started
    expect(within(approved).getByRole('button', { name: 'Override' })).not.toHaveAttribute('aria-disabled');
  });

  it('badges a migration whose numbers do not hold, and lists what the invariants found (SDD §5.5, ADV-16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!;
    m.data_issues = ['DOWNTIME_BELOW_FINAL_SYNC: 6 s of downtime around a 258 s final sync', 'BYTES_MISMATCH: 1 bytes transferred, the passes add up to 2'];
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    expect(await screen.findByText('Data inconsistent')).toBeInTheDocument();
    // the header's badge says why on hover, focus and tap, never only in a title (SDD §16)
    expect(screen.getByRole('button', { name: 'Data inconsistent' })).toHaveAccessibleDescription(
      'The numbers of this migration do not hold together: the downtime statistics leave it out.',
    );
    const notice = screen.getByRole('note', { name: 'Data inconsistent' });
    expect(notice).toHaveTextContent('The numbers of this migration do not hold together, so the downtime statistics leave it out.');
    const issues = within(notice).getAllByRole('listitem');
    expect(issues.map((li) => li.textContent)).toEqual([
      '6 s of downtime around a 258 s final sync (DOWNTIME_BELOW_FINAL_SYNC)',
      '1 bytes transferred, the passes add up to 2 (BYTES_MISMATCH)',
    ]);
  });

  it('shows no badge while the numbers hold', async () => {
    renderMigration('mig-3c1a0f9e21');
    await screen.findByRole('region', { name: /^downtime$/i });
    expect(screen.queryByText('Data inconsistent')).not.toBeInTheDocument();
  });

  it("runs the downtime clock in model time in demo mode, from GET /health's demo_speed (SDD §16, ADV-16)", async () => {
    const server = createTestServer();
    server.health = { ...server.health, demo_speed: 60 };
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e23')!;
    expect(m.phase).toBe('cutover');
    // the page's clock reads the browser's time, not the mock's
    m.downtime_started_at = new Date(Date.now() - 95_000).toISOString();
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const panel = await screen.findByRole('region', { name: /^downtime$/i });
    // stopped about 95 s ago: about 95 minutes of model time
    await waitFor(() => expect(within(panel).getByRole('timer')).toHaveTextContent(/^1:3[5-9]:\d\d$/));
    expect(panel).toHaveTextContent('Demo time: 60× faster than real time');
  });

  it("lists the estimate's parts, the final sync among them, and compares the actual downtime (SDD §16, ADV-16)", async () => {
    renderMigration('mig-3c1a0f9e21');
    const panel = await screen.findByRole('region', { name: /^downtime$/i });
    expect(within(panel).getByRole('definition', { name: 'Final sync' })).toBeInTheDocument();
    expect(within(panel).getByRole('definition', { name: 'Boot' })).toHaveTextContent('2m 00s');
    expect(panel).toHaveTextContent(/Actual 3m 32s: .* (under|over) the estimate\./);
  });

  it('names the rule that allowed the cutover on the convergence chart (SDD §5.3, ADV-16)', async () => {
    renderMigration('mig-3c1a0f9e21');
    const chart = await screen.findByRole('region', { name: /sync-pass convergence/i });
    await waitFor(() => expect(chart).toHaveTextContent('Converged on the threshold: pass 2 changed less than 1.0 GiB.'));
    expect(chart).toHaveTextContent('Final sync (pass 3), after the source stopped:');
  });

  it("shows the last verification: every probe's target, result, latency and time, and the checks (SDD §16, ADV-18)", async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!;
    const at = '2026-10-08T10:03:00Z';
    m.verification = {
      at,
      passed: true,
      checks: [
        { name: 'server_active', ok: true, detail: 'status ACTIVE' },
        { name: 'app:tcp:5432', ok: true, detail: 'connected to 10.20.0.5:5432' },
        { name: 'app:https:443', ok: false, detail: '10.20.0.5:443 unreachable (TimeoutError)', skipped: true },
      ],
      probes: [
        { target: '10.20.0.5:5432', kind: 'tcp', result: 'passed', latency_ms: 3.2, at, detail: 'connected to 10.20.0.5:5432' },
        { target: 'https://10.20.0.5:443/health', kind: 'https', result: 'unreachable', latency_ms: null, at, detail: '10.20.0.5:443 unreachable (TimeoutError)' },
      ],
      warnings: [],
    };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const panel = await screen.findByRole('region', { name: 'Verification' });
    const probes = within(panel).getByRole('table', { name: 'Application probes of the last verification' });
    const rows = within(probes).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))).toEqual([
      ['Passed', '3.2 ms', expect.any(String)],
      ['Unreachable', '—', expect.any(String)],
    ]);
    expect(within(rows[0]!).getByRole('rowheader')).toHaveTextContent('10.20.0.5:5432');
    expect(panel).toHaveTextContent('Passed');
    expect(within(panel).getByRole('list', { name: 'Checks of the last verification' })).toHaveTextContent('app:https:443');
  });

  it('says no verification ran yet, before the cutover', async () => {
    renderMigration('mig-5d7e2b4a11');
    const panel = await screen.findByRole('region', { name: 'Verification' });
    expect(panel).toHaveTextContent('Not verified yet: verification runs after the cutover.');
  });

  it("lists the VM's probes and where they come from, and lets an operator set this VM's own (SDD §16, ADV-18)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.verification = { ...plan.verification, app_probes: [{ kind: 'http', port: 80, path: '/', expect_status: null }] };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Verification' });
    await waitFor(() => expect(panel).toHaveTextContent("The plan's probes: HTTP 80 /"));

    await user.click(within(panel).getByRole('button', { name: 'Edit probes' }));
    const dialog = await screen.findByRole('dialog', { name: 'Application probes of analytics-db' });
    expect(dialog).toHaveTextContent('Saving edits the plan: it returns to draft and is validated again.');
    await user.selectOptions(within(dialog).getByRole('combobox', { name: /^probe 1 kind$/i }), 'tcp');
    const port = within(dialog).getByRole('spinbutton', { name: /^probe 1 port$/i });
    await user.clear(port);
    await user.type(port, '5432');
    await user.click(within(dialog).getByRole('button', { name: 'Save probes' }));
    await waitFor(() => expect(plan.probe_overrides[m.vm.source_id]).toEqual([{ kind: 'tcp', port: 5432, path: '/', expect_status: null }]));
    expect(plan.status).toBe('draft');
    await waitFor(() => expect(panel).toHaveTextContent("This VM's own probes: TCP 5432"));

    // back to the plan's probes: the override goes
    await user.click(within(panel).getByRole('button', { name: 'Edit probes' }));
    const again = await screen.findByRole('dialog', { name: 'Application probes of analytics-db' });
    await user.click(within(again).getByRole('button', { name: "Use the plan's probes" }));
    await waitFor(() => expect(m.vm.source_id in plan.probe_overrides).toBe(false));
  });

  it("shows where each NIC goes and lets an operator send it to another network with other addresses (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    const [nic] = m.vm.nics;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Network' });
    const table = within(panel).getByRole('table', { name: `NICs of ${m.vm.name}` });
    const row = within(table).getByRole('row', { name: new RegExp(nic!.mac!) });
    expect(row).toHaveTextContent(`${nic!.network}`);
    expect(row).toHaveTextContent(nic!.fixed_ips[0]!);

    await user.click(within(panel).getByRole('button', { name: 'Edit network' }));
    const dialog = await screen.findByRole('dialog', { name: `Network of ${m.vm.name}` });
    expect(dialog).toHaveTextContent('Saving edits the plan: it returns to draft and is validated again.');
    await user.selectOptions(within(dialog).getByRole('combobox', { name: `RHOSO network of ${nic!.mac}` }), 'tenant-web');
    await user.type(within(dialog).getByRole('textbox', { name: `Addresses of ${nic!.mac}` }), '10.20.9.9');
    await user.click(within(dialog).getByRole('button', { name: 'Save network' }));
    await waitFor(() => expect(plan.nic_overrides[m.vm.source_id]).toEqual({ [nic!.mac!]: { network: 'tenant-web', fixed_ips: ['10.20.9.9'] } }));
    expect(plan.status).toBe('draft');
    await waitFor(() => expect(within(panel).getByRole('row', { name: new RegExp(nic!.mac!) })).toHaveTextContent('tenant-web'));
    expect(within(panel).getByRole('row', { name: new RegExp(nic!.mac!) })).toHaveTextContent('10.20.9.9');
    expect(within(panel).getByRole('row', { name: new RegExp(nic!.mac!) })).toHaveTextContent('Override');

    // a bad address is named on its field, marked invalid, and gets the focus before anything is sent
    await user.click(within(panel).getByRole('button', { name: 'Edit network' }));
    const bad = await screen.findByRole('dialog', { name: `Network of ${m.vm.name}` });
    const addresses = within(bad).getByRole('textbox', { name: `Addresses of ${nic!.mac}` });
    await user.clear(addresses);
    await user.type(addresses, '10.20.9.300');
    await user.click(within(bad).getByRole('button', { name: 'Save network' }));
    await waitFor(() => expect(addresses).toHaveFocus());
    expect(addresses).toHaveAttribute('aria-invalid', 'true');
    expect(addresses).toHaveAccessibleDescription(expect.stringContaining('not an IP address: 10.20.9.300'));
    expect(plan.nic_overrides[m.vm.source_id]?.[nic!.mac!]?.fixed_ips).toEqual(['10.20.9.9']);
    await user.clear(addresses);
    await user.type(addresses, '10.20.9.10');
    expect(addresses).not.toHaveAttribute('aria-invalid');

    // back to the plan's mapping: the VM's entry goes
    await user.click(within(bad).getByRole('button', { name: "Use the plan's mapping" }));
    await waitFor(() => expect(m.vm.source_id in plan.nic_overrides).toBe(false));
  });

  it('says a VMware VM keeps its network settings, with no editor (SDD §16)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.strategy?.startsWith('vmware'))!;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Network' });
    await waitFor(() => expect(panel).toHaveTextContent("The migration kit keeps the guest's network settings"));
    expect(within(panel).queryByRole('button', { name: 'Edit network' })).not.toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: 'Remove the overrides' })).not.toBeInTheDocument();
  });

  it('offers to remove the overrides a VMware VM still has, which block it (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.strategy?.startsWith('vmware'))!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    // an editable plan: validated, nothing of it in flight
    plan.status = 'validated';
    for (const x of server.migrations.filter((y) => y.plan_id === plan.id)) Object.assign(x, { phase: 'ready', downtime_started_at: null, downtime_ended_at: null });
    plan.nic_overrides = { [m.vm.source_id]: { 'fa:16:3e:00:00:99': { network: 'tenant-web', fixed_ips: null } } };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Network' });
    await user.click(await within(panel).findByRole('button', { name: 'Remove the overrides' }));
    await waitFor(() => expect(m.vm.source_id in plan.nic_overrides).toBe(false));
    expect(plan.status).toBe('draft');
    await waitFor(() => expect(within(panel).queryByRole('button', { name: 'Remove the overrides' })).not.toBeInTheDocument());
  });

  it('says why a VMware VM cannot have its overrides removed now, and removes nothing (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.strategy?.startsWith('vmware'))!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.status = 'validated';
    for (const x of server.migrations.filter((y) => y.plan_id === plan.id)) Object.assign(x, { phase: 'ready', downtime_started_at: null, downtime_ended_at: null });
    const entries = { 'fa:16:3e:00:00:99': { network: 'tenant-web', fixed_ips: null } };
    plan.nic_overrides = { [m.vm.source_id]: entries };
    // a viewer: the role says why
    const viewer = renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    let panel = await screen.findByRole('region', { name: 'Network' });
    let remove = await within(panel).findByRole('button', { name: 'Remove the overrides' });
    await waitFor(() => expect(remove).toHaveAccessibleDescription('Requires the operator role.'));
    await user.click(remove);
    expect(plan.nic_overrides[m.vm.source_id]).toEqual(entries);
    viewer.unmount();
    // an operator while a migration of the plan is in flight: the plan's edit rule says why
    m.phase = 'syncing';
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    panel = await screen.findByRole('region', { name: 'Network' });
    remove = await within(panel).findByRole('button', { name: 'Remove the overrides' });
    await waitFor(() => expect(remove).toHaveAccessibleDescription(expect.stringContaining(m.vm.name)));
    await user.click(remove);
    expect(plan.nic_overrides[m.vm.source_id]).toEqual(entries);
  });

  it("keeps an override's network that RHOSO no longer lists selected, marked so (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    const [nic] = m.vm.nics;
    plan.nic_overrides = { [m.vm.source_id]: { [nic!.mac!]: { network: 'rhoso-gone', fixed_ips: null } } };
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Network' });
    await user.click(await within(panel).findByRole('button', { name: 'Edit network' }));
    const dialog = await screen.findByRole('dialog', { name: `Network of ${m.vm.name}` });
    const select = within(dialog).getByRole('combobox', { name: `RHOSO network of ${nic!.mac}` });
    expect(await within(select).findByRole('option', { name: 'rhoso-gone (not in RHOSO)' })).toBeInTheDocument();
    expect(select).toHaveValue('rhoso-gone');
    // said under the field too: a narrow select cuts its option's text
    expect(select).toHaveAccessibleDescription('RHOSO does not list rhoso-gone: validation refuses it.');
    await user.click(within(dialog).getByRole('button', { name: 'Save network' }));
    await waitFor(() => expect(plan.status).toBe('draft'));
    expect(plan.nic_overrides[m.vm.source_id]).toEqual({ [nic!.mac!]: { network: 'rhoso-gone', fixed_ips: null } });
  });

  it('announces why Save probes was refused from a live region already in the dialog (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Verification' });
    await user.click(await within(panel).findByRole('button', { name: 'Edit probes' }));
    const dialog = await screen.findByRole('dialog', { name: 'Application probes of analytics-db' });
    await user.click(within(dialog).getByRole('button', { name: 'Add probe' }));
    const port = within(dialog).getAllByRole('spinbutton', { name: /^probe \d+ port$/i }).at(-1)!;
    await user.clear(port);
    await user.type(port, '70000');
    const regions = within(dialog).getAllByRole('status');
    const before = structuredClone(plan.probe_overrides);
    await user.click(within(dialog).getByRole('button', { name: 'Save probes' }));
    const error = await within(dialog).findByText(/use a port from 1 to 65535\./);
    // a live region inserted together with its text is not reliably read
    expect(regions).toContain(error.closest('[role="status"]'));
    expect(plan.probe_overrides).toEqual(before);
  });

  it('moves focus to Edit probes when Use suggested probe takes its suggestion away (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-node-1')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.verification = { ...plan.verification, app_probes: [] };
    m.suggested_probes = [{ kind: 'tcp', port: 5432, path: '/', expect_status: null }];
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const panel = await screen.findByRole('region', { name: 'Verification' });
    const use = await within(panel).findByRole('button', { name: 'Use suggested probe' });
    await user.click(use);
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Probe analytics-node-1 on TCP 5432?' })).getByRole('button', { name: 'Cancel' }));
    expect(use).toHaveFocus();
    await user.click(use);
    const dialog = await screen.findByRole('alertdialog', { name: 'Probe analytics-node-1 on TCP 5432?' });
    await user.click(within(dialog).getByRole('button', { name: 'Use suggested probe' }));
    await waitFor(() => expect(plan.probe_overrides[m.vm.source_id]).toEqual(m.suggested_probes));
    await waitFor(() => expect(use).not.toBeInTheDocument());
    const edit = within(panel).getByRole('button', { name: 'Edit probes' });
    expect(edit).toHaveFocus();

    // back to the plan's (none): the suggestion returns, and a Cancel keeps focus on its button
    await user.click(edit);
    await user.click(within(await screen.findByRole('dialog', { name: 'Application probes of analytics-node-1' })).getByRole('button', { name: "Use the plan's probes" }));
    const again = await within(panel).findByRole('button', { name: 'Use suggested probe' });
    await user.click(again);
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Probe analytics-node-1 on TCP 5432?' })).getByRole('button', { name: 'Cancel' }));
    expect(again).toHaveFocus();
  });

  it('keeps Edit probes from a viewer, saying which role it needs', async () => {
    renderMigration('mig-5d7e2b4a11');
    const panel = await screen.findByRole('region', { name: 'Verification' });
    // the probes show once the plan has loaded
    const edit = await within(panel).findByRole('button', { name: 'Edit probes' });
    expect(edit).toHaveAttribute('aria-disabled', 'true');
    expect(edit).toHaveAccessibleDescription('Requires the operator role.');
  });

  it('alerts while the source VM of a completed migration runs again (SDD §8.1, §16, ADV-19)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'web-cache-01')!;
    m.source_running_at = '2026-10-08T11:00:00Z';
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const alert = await screen.findByRole('alert', { name: 'Source VM running' });
    expect(alert).toHaveTextContent('The source VM runs again since');
    expect(alert).toHaveTextContent('its data diverges from RHOSO: stop it, or roll back.');
    // a viewer sees the demo control, and why it is not theirs
    const power = await within(screen.getByRole('region', { name: 'Next step' })).findByRole('button', { name: 'Power off the source VM (demo)' });
    expect(power).toHaveAttribute('aria-disabled', 'true');
    expect(power).toHaveAccessibleDescription('Requires the operator role.');
  });

  it('says how long a completed migration has waited, and asks for finalize past the plan reminder (ADV-19)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'web-cache-01')!; // completed 8 days ago
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    const step = await screen.findByRole('region', { name: 'Next step' });
    await waitFor(() => expect(step).toHaveTextContent('It has waited in Completed longer than the plan allows (72 h): finalize it or roll it back.'));
  });

  it('lets an operator power the source VM on and off in demo mode, to show the alert (SDD §8.1, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'web-cache-01')!;
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const step = await screen.findByRole('region', { name: 'Next step' });
    await user.click(await within(step).findByRole('button', { name: 'Power on the source VM (demo)' }));
    expect(await within(step).findByRole('button', { name: 'Power off the source VM (demo)' })).toBeInTheDocument();
    expect(server.sourcePower(m.vm.source_id)).toBe('running');
    // the next source watch flags it
    server.tick();
    expect(m.source_running_at).not.toBeNull();
  });

  it('names the kind of every approval, the advisor decision among them (SDD §16, ADV-20)', async () => {
    const server = createTestServer();
    const held = server.migrations.find((m) => m.vm.name === 'analytics-node-2')!;
    const route = (m: Migration) => ({ route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'viewer', server });
    let view = renderWithApp(<MigrationDetail />, route(held));
    let approvals = await screen.findByRole('region', { name: 'Approvals' });
    await waitFor(() => expect(approvals).toHaveTextContent('Advisor decision · Migration strategy'));
    expect(approvals).toHaveTextContent('Waits for approval (admin): approve or override it on its recommendation.');
    expect(within(approvals).getByRole('link', { name: 'its recommendation' })).toHaveAttribute('href', `/advisor/recommendations/${held.advisor_hold}`);
    // its cutover approval is a kind of its own, not given yet
    expect(approvals).toHaveTextContent('No cutover approvals yet.');
    view.unmount();

    // once a person decided: who, and how
    await createTestClient(server, 'admin').post(`/advisor/recommendations/${held.advisor_hold}/approve`, {});
    view = renderWithApp(<MigrationDetail />, route(held));
    approvals = await screen.findByRole('region', { name: 'Approvals' });
    await waitFor(() => expect(approvals).toHaveTextContent('Advisor decision · Migration strategy'));
    expect(approvals).toHaveTextContent('Approved by rina');
    view.unmount();

    // a cutover approval says its kind too
    const erp = server.migrations.find((m) => m.vm.name === 'vm-erp-db-01')!;
    expect(erp.approvals.map((a) => a.actor)).toEqual(['sari']);
    renderWithApp(<MigrationDetail />, route(erp));
    approvals = await screen.findByRole('region', { name: 'Approvals' });
    await waitFor(() => expect(within(approvals).getAllByText('Cutover approval')).toHaveLength(1));
    expect(approvals).toHaveTextContent('Approved by sari');
  });

  it('lands on its findings or estimates when a link names them (SDD §16, ADV-02)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    for (const part of ['findings', 'estimates'] as const) {
      const view = renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}#${part}`, path: '/migrations/:migrationId', token: 'viewer', server });
      await screen.findByRole('heading', { level: 1, name: 'analytics-db' });
      const target = await waitFor(() => {
        const element = document.getElementById(part);
        expect(element).not.toBeNull();
        return element!;
      });
      await waitFor(() => expect(target).toHaveFocus());
      expect(within(target).getByRole('heading', { level: 2, name: part === 'findings' ? 'Findings' : 'Estimates' })).toBeInTheDocument();
      view.unmount();
    }
  });

  it('shows no demo power control outside demo mode', async () => {
    const server = createTestServer();
    server.health = { ...server.health, demo: false, demo_speed: null };
    const m = server.migrations.find((x) => x.vm.name === 'web-cache-01')!;
    const { queryClient } = renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });
    const step = await screen.findByRole('region', { name: 'Next step' });
    await within(step).findByRole('group', { name: /migration actions/i });
    // once the control plane said it is not in demo mode
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.health)).toMatchObject({ demo: false }));
    expect(within(step).queryByRole('button', { name: /source VM \(demo\)/ })).not.toBeInTheDocument();
  });
});
