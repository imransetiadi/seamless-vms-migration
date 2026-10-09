import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderWithApp } from '../test/utils';
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
});
