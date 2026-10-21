import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import Providers from '../pages/Providers';
import { createTestServer, renderWithApp } from '../test/utils';
import { TECH_PREVIEW_DETAIL } from './TechPreviewBadge';

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

describe('ProviderDialog: declared vGPU profiles (SDD §16, §22.3; plan task G11)', () => {
  /** The flavors a Flavor field suggests: the options of its datalist. */
  const offered = (field: HTMLElement): string[] => {
    const list = field.getAttribute('list');
    return list ? [...document.getElementById(list)!.querySelectorAll('option')].map((option) => option.value) : [];
  };

  it('takes declared vGPU profiles for an OpenStack or RHOSO provider', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    // an OpenStack destination: the pairs it declares, and its GPU flavors once its inventory is read
    const hibiscus = await screen.findByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    await user.click(within(hibiscus).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog');
    let section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    expect(within(section).getByRole('button', { name: 'Technology Preview' })).toHaveAccessibleDescription(TECH_PREVIEW_DETAIL);
    expect(within(section).getByLabelText('Flavor 1')).toHaveValue('g1.a100-4c');
    expect(within(section).getByLabelText('Profile 1')).toHaveValue('A100-4C');
    expect(within(section).getByLabelText('Flavor 2')).toHaveValue('g1.a100-3-20c');
    expect(within(section).getByLabelText('Profile 2')).toHaveValue('A100-3-20C');
    expect(await within(section).findByText('Its GPU flavors: g1.a100-3-20c, g1.a100-4c and p1.a100. Each Flavor field offers those not declared yet.')).toBeInTheDocument();
    // each Flavor field offers the GPU flavors not declared yet
    expect(offered(within(section).getByLabelText('Flavor 1'))).toEqual(['p1.a100']);
    // A100-3-20C goes; the alias flavor declares the vGPU a vendor-specific VFIO framework hands out as a PCI device (§22.3)
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 2' }));
    expect(within(section).getByRole('heading', { name: 'vGPU profiles' })).toHaveFocus();
    expect(offered(within(section).getByLabelText('Flavor 1'))).toEqual(['g1.a100-3-20c', 'p1.a100']);
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 2'), 'p1.a100');
    await user.type(within(section).getByLabelText('Profile 2'), ' A100D-80C ');
    expect(offered(within(section).getByLabelText('Flavor 2'))).toEqual(['g1.a100-3-20c']);
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // the map whole, each name trimmed (SDD §12)
    expect(server.providers.find((p) => p.id === 'hibiscus-dc3')?.gpu_profiles).toEqual({ 'g1.a100-4c': 'A100-4C', 'p1.a100': 'A100D-80C' });
    expect(server.events.filter((e) => e.kind === 'provider.updated').at(-1)?.data.fields).toContain('gpu_profiles');

    // RHOSO 18.0: nothing declared, and no GPU flavor in its inventory - a flavor is typed
    const staging = screen.getByRole('article', { name: 'RHOSO 18.0 — Staging' });
    await user.click(within(staging).getByRole('button', { name: /^edit /i }));
    dialog = await screen.findByRole('dialog');
    section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    expect(section).toHaveTextContent('No vGPU profile declared.');
    expect(await within(section).findByText("Its inventory lists no flavor that asks for a GPU: type each flavor's name.")).toBeInTheDocument();
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    expect(within(section).getByLabelText('Flavor 1')).not.toHaveAttribute('list');
    await user.type(within(section).getByLabelText('Flavor 1'), 'g2.l40s-12q');
    await user.type(within(section).getByLabelText('Profile 1'), 'L40S-12Q');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.providers.find((p) => p.id === 'rhoso-staging')?.gpu_profiles).toEqual({ 'g2.l40s-12q': 'L40S-12Q' });

    // a new OpenStack provider declares them as it is added; it offers no GPU flavor before its inventory can be read
    await user.click(screen.getByRole('button', { name: /^add provider$/i }));
    dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    expect(section).toHaveTextContent('A provider not added yet offers no GPU flavor: its inventory is read once it is added.');
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 1'), 'g1.vgpu');
    await user.type(within(section).getByLabelText('Profile 1'), 'A100-4C');
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla GPU');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-gpu.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.providers.find((p) => p.id === 'kolla-gpu')).toMatchObject({ kind: 'openstack', role: 'source', gpu_profiles: { 'g1.vgpu': 'A100-4C' } });
  });

  it('offers no vGPU profiles for a vCenter', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const created: unknown[] = [];
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      if (method === 'POST' && path === '/providers') created.push(body);
      return handle(method, path, query, body, token);
    };
    renderPage(server);
    // vCenter reports each VM's own vGPU profile (§22.3): its form has none
    const hq = await screen.findByRole('article', { name: 'vCenter HQ' });
    await user.click(within(hq).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog', { name: /edit vcenter hq/i });
    expect(within(dialog).queryByRole('region', { name: 'vGPU profiles' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Add vGPU profile' })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    // pairs typed for an OpenStack platform go once vCenter is chosen, and the new vCenter is sent none
    await user.click(screen.getByRole('button', { name: /^add provider$/i }));
    dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    const section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 1'), 'g1.a100-4c');
    await user.type(within(section).getByLabelText('Profile 1'), 'A100-4C');
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    expect(within(dialog).queryByRole('region', { name: 'vGPU profiles' })).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC7');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc7.example.com/sdk');
    await user.click(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^secret name/i), 'vcenter-dc7');
    await user.click(within(dialog).getByRole('button', { name: /existing host/i }));
    await user.type(within(dialog).getByLabelText(/^address/i), '198.51.100.79');
    await user.click(within(dialog).getByRole('radio', { name: 'Key in a secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^key secret name/i), 'conv-dc7-key');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(created).toHaveLength(1);
    expect(created[0]).toMatchObject({ id: 'vcenter-dc7', kind: 'vmware' });
    expect(created[0]).not.toHaveProperty('gpu_profiles');
  });

  it('refuses what the API would refuse before anything is sent, and sends the profiles only when they changed (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const lab = server.providers.find((p) => p.id === 'community-lab')!;
    lab.gpu_profiles = { 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' };
    renderPage(server);
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    const section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    // its cloud answers HTTP 503: nothing is offered, and nothing reads as all clear
    expect(await within(section).findByText("Its inventory could not be read, so no GPU flavor is offered: type each flavor's name.")).toBeInTheDocument();
    // a flavor declared twice, an empty flavor and profile, and a profile over 64 characters are named, nothing sent
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 3'), 'g1.a100-4c');
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.click(within(section).getByLabelText('Profile 4'));
    await user.paste('A'.repeat(65));
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(within(summary).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'g1.a100-4c is declared twice: change or remove vGPU profile 3.',
      'Enter the profile of vGPU profile 3, such as A100-4C.',
      'Enter the flavor of vGPU profile 4, or remove it.',
      'Shorten the profile of vGPU profile 4: at most 64 characters.',
    ]);
    await waitFor(() => expect(summary).toHaveFocus());
    expect(server.events.some((e) => e.kind === 'provider.updated')).toBe(false);
    // each line leads to its field, which says its problem
    await user.click(within(summary).getByRole('link', { name: /^Shorten the profile/ }));
    const long = within(section).getByLabelText('Profile 4');
    expect(long).toHaveFocus();
    expect(long).toHaveAttribute('aria-invalid', 'true');
    expect(long).toHaveAccessibleDescription('Shorten the profile of vGPU profile 4: at most 64 characters.');
    // a removed row takes its problems with it
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 4' }));
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 3' }));
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    // a pair removed and declared again is the same map in another order: a PATCH would reset the check for nothing
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 1' }));
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 2'), 'g1.a100-4c');
    await user.type(within(section).getByLabelText('Profile 2'), 'A100-4C');
    const region = within(dialog).getByLabelText(/^region/i);
    await user.clear(region);
    await user.type(region, 'RegionTwo');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.updated').at(-1)?.data).toEqual({ provider_id: 'community-lab', fields: ['region'] });
    expect(server.providers.find((p) => p.id === 'community-lab')?.gpu_profiles).toEqual({ 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' });
  });
});
