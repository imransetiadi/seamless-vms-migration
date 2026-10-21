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
  /** Every request the page sends, and the bodies of its writes. */
  const record = (server: ReturnType<typeof createTestServer>) => {
    const calls: string[] = [];
    const bodies: Array<{ method: string; path: string; body: unknown }> = [];
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      calls.push(`${method} ${path}`);
      if (method !== 'GET') bodies.push({ method, path, body });
      return handle(method, path, query, body, token);
    };
    return { calls, bodies };
  };
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
    const { calls, bodies } = record(server);
    renderPage(server);
    // vCenter reports each VM's own vGPU profile (§22.3): its form has none, and reads no inventory for them
    const hq = await screen.findByRole('article', { name: 'vCenter HQ' });
    await user.click(within(hq).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog', { name: /edit vcenter hq/i });
    expect(within(dialog).queryByRole('region', { name: 'vGPU profiles' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Add vGPU profile' })).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).not.toContain('GET /providers/vcenter-hq/inventory');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    // pairs typed for an OpenStack platform go once vCenter is chosen - their problems too -, and none is sent
    await user.click(screen.getByRole('button', { name: /^add provider$/i }));
    dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    const section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.type(within(section).getByLabelText('Flavor 1'), 'g1.a100-4c');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter the profile of vGPU profile 1, such as A100-4C.');
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    expect(within(dialog).queryByRole('region', { name: 'vGPU profiles' })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('alert')).not.toHaveTextContent(/vGPU profile/);
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC7');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc7.example.com/sdk');
    await user.click(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^secret name/i), 'vcenter-dc7');
    await user.click(within(dialog).getByRole('button', { name: /existing host/i }));
    await user.type(within(dialog).getByLabelText(/^address/i), '198.51.100.79');
    await user.click(within(dialog).getByRole('radio', { name: 'Key in a secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^key secret name/i), 'conv-dc7-key');
    // the hidden row, its profile still empty, is judged no more
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const created = bodies.filter((b) => b.method === 'POST' && b.path === '/providers').map((b) => b.body);
    expect(created).toHaveLength(1);
    expect(created[0]).toMatchObject({ id: 'vcenter-dc7', kind: 'vmware' });
    expect(created[0]).not.toHaveProperty('gpu_profiles');
  });

  it('refuses what the API would refuse before anything is sent, and sends the profiles only when they changed (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const { bodies } = record(server);
    const lab = server.providers.find((p) => p.id === 'community-lab')!;
    lab.gpu_profiles = { 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' };
    renderPage(server);
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    let section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
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
    const problems = () => within(within(dialog).getByRole('alert')).getAllByRole('link').map((link) => link.textContent);
    expect(problems()).toEqual([
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
    // a Flavor field's own problem comes first, then the section's hint
    expect(within(section).getByLabelText('Flavor 3')).toHaveAccessibleDescription(
      "g1.a100-4c is declared twice: change or remove vGPU profile 3. Its inventory could not be read, so no GPU flavor is offered: type each flavor's name.",
    );
    // a removed row takes its problems with it, and the rows left are judged again: no twin is left, the rows renumber
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 4' }));
    expect(problems()).toEqual(['g1.a100-4c is declared twice: change or remove vGPU profile 3.', 'Enter the profile of vGPU profile 3, such as A100-4C.']);
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 1' }));
    expect(problems()).toEqual(['Enter the profile of vGPU profile 2, such as A100-4C.']);
    expect(within(section).getByLabelText('Flavor 2')).not.toHaveAttribute('aria-invalid');
    // the pair removed and declared again is the same map in another order: a PATCH would reset the check for nothing
    await user.type(within(section).getByLabelText('Profile 2'), 'A100-4C');
    const region = within(dialog).getByLabelText(/^region/i);
    await user.clear(region);
    await user.type(region, 'RegionTwo');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.updated').at(-1)?.data).toEqual({ provider_id: 'community-lab', fields: ['region'] });
    expect(server.providers.find((p) => p.id === 'community-lab')?.gpu_profiles).toEqual({ 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' });
    // every pair removed: the map is sent empty, which clears the declaration
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 2' }));
    await user.click(within(section).getByRole('button', { name: 'Remove vGPU profile 1' }));
    expect(section).toHaveTextContent('No vGPU profile declared.');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodies.filter((b) => b.method === 'PATCH').at(-1)?.body).toEqual({ gpu_profiles: {} });
    expect(server.providers.find((p) => p.id === 'community-lab')?.gpu_profiles).toEqual({});
  });

  it('reads the inventory while the form shows and nothing saves, says so while it reads, and describes each Flavor field with it', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const { calls } = record(server);
    const reads = (id: string) => calls.filter((call) => call === `GET /providers/${id}/inventory`).length;
    // the read waits until the test lets it through
    let release: () => void = () => {};
    let hold: Promise<void> | null = new Promise<void>((resolve) => (release = resolve));
    const wrapFetch = (fetchImpl: typeof fetch): typeof fetch => async (input, init) => {
      if (hold && String(input).endsWith('/inventory')) await hold;
      return fetchImpl(input, init);
    };
    renderWithApp(<Providers />, { route: '/providers', path: '/providers', token: 'admin', server, wrapFetch });
    const hibiscus = await screen.findByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    await user.click(within(hibiscus).getByRole('button', { name: /^edit /i }));
    let dialog = await screen.findByRole('dialog');
    const section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    expect(within(section).getByText('Reading its inventory for its GPU flavors.')).toBeInTheDocument();
    expect(within(section).getByLabelText('Flavor 1')).toHaveAccessibleDescription('Reading its inventory for its GPU flavors.');
    release();
    hold = null;
    const offered = 'Its GPU flavors: g1.a100-3-20c, g1.a100-4c and p1.a100. Each Flavor field offers those not declared yet.';
    expect(await within(section).findByText(offered)).toBeInTheDocument();
    expect(within(section).getByLabelText('Flavor 2')).toHaveAccessibleDescription(offered);
    expect(reads('hibiscus-dc3')).toBe(1);
    // a save's steps and the test's result read nothing: for a source the read lists every VM, for a destination it asks Placement
    const region = within(dialog).getByLabelText(/^region/i);
    await user.clear(region);
    await user.type(region, 'RegionTwo');
    await user.click(within(dialog).getByRole('button', { name: /^save and test connection$/i }));
    const result = await screen.findByRole('dialog', { name: /is connected|did not pass/i });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(reads('hibiscus-dc3')).toBe(1);
    await user.click(within(result).getByRole('button', { name: /^done$/i }));
    // nor does a new provider's: added, its credentials stored and tested, it has not been read
    await user.click(screen.getByRole('button', { name: /^add provider$/i }));
    dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla GPU');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-gpu.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /^add and test connection$/i }));
    await screen.findByRole('dialog', { name: /kolla gpu is connected/i });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(reads('kolla-gpu')).toBe(0);
  });
});
