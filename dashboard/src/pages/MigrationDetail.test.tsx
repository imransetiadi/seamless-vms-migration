import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { STRATEGY_LABELS } from '../lib/status';
import { createTestServer, renderWithApp } from '../test/utils';
import MigrationDetail from './MigrationDetail';

function renderMigration(id: string) {
  return renderWithApp(<MigrationDetail />, { route: `/migrations/${id}`, path: '/migrations/:migrationId', token: 'viewer' });
}

describe('MigrationDetail', () => {
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
    expect(await screen.findByText(/no approvals yet|does not require approval/i)).toBeInTheDocument();
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
});
