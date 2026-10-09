import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
});
