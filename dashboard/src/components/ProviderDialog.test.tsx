import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import Providers from '../pages/Providers';
import { createTestServer, renderWithApp } from '../test/utils';

function renderPage(server = createTestServer()) {
  return renderWithApp(<Providers />, { route: '/providers', path: '/providers', token: 'admin', server });
}

describe('ProviderDialog: what a destination declares (SDD §4.2, §16; plan T8)', () => {
  it('takes a declared series for a community destination', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    await user.click(await screen.findByRole('button', { name: /^add provider$/i }));
    const dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    // a source declares neither: the series and profile are a destination's
    expect(within(dialog).queryByLabelText(/^release series/i)).toBeNull();
    expect(within(dialog).queryByLabelText(/^policy profile/i)).toBeNull();
    await user.click(within(dialog).getByRole('radio', { name: /migrate to this cloud/i }));
    const series = within(dialog).getByLabelText(/^release series/i);
    // blank detects it; the window's series, newest first
    expect([...series.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Detect it from the APIs',
      '2026.2 Hibiscus',
      '2026.1 Gazpacho',
      '2025.2 Flamingo',
      '2025.1 Epoxy',
      '2024.2 Dalmatian',
      '2024.1 Caracal',
      '2023.2 Bobcat',
      '2023.1 Antelope',
    ]);
    await user.selectOptions(series, '2026.2');
    await user.selectOptions(within(dialog).getByLabelText(/^policy profile/i), 'secure_rbac');
    await user.type(within(dialog).getByLabelText(/^name/i), 'Hibiscus DC4');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-dc4.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    await waitFor(() => expect(server.providers.find((p) => p.id === 'hibiscus-dc4')).toMatchObject({ role: 'destination', release: '2026.2', policy_profile: 'secure_rbac' }));
  });

  it('edits a declared series back to detection, and takes a profile but no series for RHOSO 18.0', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const hibiscus = await screen.findByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    await user.click(within(hibiscus).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/^release series/i)).toHaveValue('2026.2');
    expect(within(dialog).getByLabelText(/^policy profile/i)).toHaveValue('');
    await user.selectOptions(within(dialog).getByLabelText(/^release series/i), '');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(server.providers.find((p) => p.id === 'hibiscus-dc3')?.release).toBeNull());
    // RHOSO 18.0 is validated by its kind: it declares a profile (D14), never a series (a provider no running plan uses)
    const rhoso = await screen.findByRole('article', { name: 'RHOSO 18.0 — Staging' });
    await user.click(within(rhoso).getByRole('button', { name: /^edit /i }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText(/^release series/i)).toBeNull();
    await user.selectOptions(within(dialog).getByLabelText(/^policy profile/i), 'legacy');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(server.providers.find((p) => p.id === 'rhoso-staging')?.policy_profile).toBe('legacy'));
  });
});
