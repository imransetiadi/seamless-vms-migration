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
    const user = userEvent.setup();
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
    const user = userEvent.setup();
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
    const user = userEvent.setup();
    renderWithApp(<MigrationDetail />, { route: `/migrations/${m.id}`, path: '/migrations/:migrationId', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: `Use ${STRATEGY_LABELS[other]}` }));
    await waitFor(() => expect(m.strategy).toBe(other));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});
