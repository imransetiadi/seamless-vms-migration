import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, recordScrolls, renderWithApp } from '../test/utils';
import Providers from './Providers';

// a key-shaped string without key material (write-only upload fixture)
const KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n-----END OPENSSH PRIVATE KEY-----\n'; // gitleaks:allow

function renderPage(token = 'admin', server = createTestServer()) {
  return renderWithApp(<Providers />, { route: '/providers', path: '/providers', token, server });
}

async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: /^add provider$/i }));
  return screen.findByRole('dialog', { name: /^add provider$/i });
}

describe('Providers page', () => {
  it('names the add dialog as its button: the action keeps one name through its flow', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('admin');
    await user.click(await screen.findByRole('button', { name: /^add provider$/i }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAccessibleName('Add provider');
    expect(within(dialog).getByRole('button', { name: 'Add provider' })).toBeInTheDocument();
  });

  it('shows sources and destinations in the direction of the migration, with the sign-in state', async () => {
    renderPage('viewer');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const to = screen.getByRole('region', { name: 'Migrate to' });
    expect(within(from).getByRole('heading', { name: 'RHOSP 17.1 — DC1' })).toBeInTheDocument();
    expect(within(from).getByRole('heading', { name: 'Kolla Edge (Bobcat)' })).toBeInTheDocument();
    expect(within(to).getByRole('heading', { name: 'RHOSO 18.0 — Production' })).toBeInTheDocument();
    const rhosp = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    expect(rhosp).toHaveTextContent('Red Hat OpenStack 17.1');
    expect(rhosp).toHaveTextContent(/stored .* ago/i);
    expect(within(from).getByRole('article', { name: 'Kolla Edge (Bobcat)' })).toHaveTextContent(/not set\. edit the provider to add sign-in details/i);
    // the fleet summary counts every provider by status and those without sign-in details
    const summary = screen.getByRole('list', { name: /providers by status/i });
    expect(summary).toHaveTextContent(/healthy\s*2/i);
    expect(screen.getByText(/1 without sign-in details/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check all/i })).toHaveAttribute('aria-disabled', 'true');
    // viewers learn the actions exist but cannot use them
    expect(screen.getByRole('button', { name: /^add provider$/i })).toHaveAttribute('aria-disabled', 'true');
    expect(within(rhosp).getByRole('button', { name: /^edit /i })).toHaveAttribute('aria-disabled', 'true');
  });

  it('keeps Cancel focusable while the provider is saved, saying why (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Providers />, {
      route: '/providers',
      path: '/providers',
      token: 'admin',
      // the save never answers: the dialog stays busy
      wrapFetch: (mock) => (input, init) => ((init?.method ?? 'GET') === 'GET' ? mock(input, init) : new Promise<Response>(() => {})),
    });
    const rhosp = await screen.findByRole('article', { name: 'RHOSP 17.1 — DC1' });
    await user.click(within(rhosp).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    await waitFor(() => expect(cancel).toHaveAttribute('aria-disabled', 'true'));
    expect(cancel).not.toBeDisabled();
    expect(cancel).toHaveAccessibleDescription('Wait for the request to finish.');
  });

  it('names its provider on each card control: a list of the page controls never reads "Edit" three times (SDD §16)', async () => {
    const server = createTestServer();
    renderPage('admin', server);
    await screen.findByRole('article', { name: server.providers[0]!.name });
    expect(server.providers.length).toBeGreaterThan(1);
    for (const provider of server.providers) {
      const card = screen.getByRole('article', { name: provider.name });
      // the visible label first, then the provider the card's heading shows
      for (const verb of ['Check', 'Edit', 'Delete']) {
        expect(within(card).getByRole('button', { name: `${verb} ${provider.name}` })).toHaveTextContent(new RegExp(`^${verb}`));
      }
      expect(within(card).getByRole('link', { name: `Inventory of ${provider.name}` })).toHaveAttribute('href', `/inventory/${provider.id}`);
    }
    for (const bare of ['Check', 'Edit', 'Delete']) expect(screen.queryByRole('button', { name: bare })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Inventory' })).not.toBeInTheDocument();
  });

  it('shows Delete to non-admins as unavailable with the reason, like Edit (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('operator');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const rhosp = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    const remove = within(rhosp).getByRole('button', { name: /^delete /i });
    expect(remove).toHaveAttribute('aria-disabled', 'true');
    expect(remove).toHaveAccessibleDescription('Deleting a provider requires the admin role.');
    await user.click(remove);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('lists the storage backends of a cloud by driver family instead of raw pool names', async () => {
    renderPage('viewer');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const rhosp = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    const caps = within(rhosp).getByRole('list', { name: /capabilities/i });
    expect(caps).toHaveTextContent('Storage');
    expect(caps).toHaveTextContent('Ceph RBD (4 pools), NetApp ONTAP NFS (1 pool)');
    expect(caps).not.toHaveTextContent('[object Object]');
    expect(caps).not.toHaveTextContent('Volume backends');
  });

  it('checks every provider at once for an operator', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage('operator');
    const before = server.events.filter((e) => e.kind === 'provider.checked').length;
    await user.click(await screen.findByRole('button', { name: /check all/i }));
    await waitFor(() => expect(screen.getByText(/checked 6 providers/i)).toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.checked')).toHaveLength(before + 6);
  });

  it('names a provider whose check request failed, counts it, and retries only that one (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const failing = server.providers[0]!;
    const total = server.providers.length;
    const handle = server.handle.bind(server);
    let refuse = true;
    // the request fails (not the cloud): the provider's own status does not change
    server.handle = (method, path, query, body, token) =>
      refuse && method === 'POST' && path === `/providers/${failing.id}/check`
        ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
        : handle(method, path, query, body, token);
    renderPage('operator', server);
    await user.click(await screen.findByRole('button', { name: /check all/i }));

    // not left out as if it had not been asked: named, with the reason and Retry
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(failing.name);
    expect(alert).toHaveTextContent(/database unavailable/i);
    const healthy = server.providers.filter((p) => p.id !== failing.id && p.status === 'ok').length;
    expect(screen.getByText(`Checked ${total - 1} of ${total} providers: ${healthy} healthy; 1 could not be checked.`)).toBeInTheDocument();

    // Retry checks that provider only
    refuse = false;
    const before = server.events.filter((e) => e.kind === 'provider.checked').length;
    await user.click(within(alert).getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.getByText(/^Checked 1 provider: /)).toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.checked')).toHaveLength(before + 1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([2, 6])('names %i providers whose check requests failed, at most three by name (SDD §16)', async (count) => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const total = server.providers.length;
    const failing = server.providers.slice(0, count);
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'POST' && failing.some((p) => path === `/providers/${p.id}/check`)
        ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
        : handle(method, path, query, body, token);
    renderPage('operator', server);
    await user.click(await screen.findByRole('button', { name: /check all/i }));
    const names = failing.map((p) => p.name);
    const expected = count > 3 ? `${names.slice(0, 3).join(', ')} and ${count - 3} more` : `${names.slice(0, -1).join(', ')} and ${names[count - 1]}`;
    expect(await screen.findByRole('alert')).toHaveTextContent(`Could not check ${expected}`);
    const healthy = server.providers.filter((p) => !failing.includes(p) && p.status === 'ok').length;
    expect(screen.getByText(`Checked ${total - count} of ${total} providers: ${healthy} healthy; ${count} could not be checked.`)).toBeInTheDocument();
  });

  it('keeps the failed checks named while Retry runs, and Retry keeps the focus (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const failing = server.providers[0]!;
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'POST' && path === `/providers/${failing.id}/check`
        ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
        : handle(method, path, query, body, token);
    // the retried check waits until the test lets it through
    let hold: Promise<void> | null = null;
    let release: () => void = () => {};
    const wrapFetch = (fetchImpl: typeof fetch): typeof fetch => async (input, init) => {
      if (hold && String(input).endsWith(`/providers/${failing.id}/check`)) await hold;
      return fetchImpl(input, init);
    };
    renderWithApp(<Providers />, { route: '/providers', path: '/providers', token: 'operator', server, wrapFetch });
    await user.click(await screen.findByRole('button', { name: /check all/i }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(`Could not check ${failing.name}`);
    const retry = within(alert).getByRole('button', { name: /retry/i });
    hold = new Promise<void>((resolve) => (release = resolve));
    await user.click(retry);
    // while it runs, the failure stays named and Retry is busy with the focus
    await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'));
    expect(screen.getByRole('alert')).toHaveTextContent(`Could not check ${failing.name}`);
    expect(retry).toHaveFocus();
    release();
    // it failed again: the same banner, Retry ready again and still focused
    await waitFor(() => expect(retry).not.toHaveAttribute('aria-busy'));
    expect(screen.getByRole('alert')).toBe(alert);
    expect(retry).toHaveFocus();
  });

  it('summarises what is missing and focuses the summary', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/fields need attention/i);
    for (const text of [/recognise/i, /full url/i, /user name/i, /password/i, /project the user signs in to/i]) {
      expect(summary).toHaveTextContent(text);
    }
    await waitFor(() => expect(summary).toHaveFocus());
    // each line takes the operator to its field, as in the plan and policy dialogs
    await user.click(within(summary).getByRole('link', { name: /full url/i }));
    expect(within(dialog).getByLabelText(/keystone url/i)).toHaveFocus();
  });

  it('connects a Kolla-Ansible source: the preset fills the form, credentials stay write-only', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    expect(within(dialog).getByLabelText(/keystone url/i)).toHaveAttribute('placeholder', 'e.g. https://kolla-vip.example.com:5000/v3');
    expect(within(dialog).getByText(/root\.crt/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla Lab B');
    expect(within(dialog).getByLabelText(/^id/i)).toHaveValue('kolla-lab-b');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-b.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));

    const result = await screen.findByRole('dialog', { name: /kolla lab b is connected/i });
    expect(result).toHaveTextContent(/all services reachable/i);
    const created = server.providers.find((p) => p.id === 'kolla-lab-b');
    expect(created).toMatchObject({ kind: 'openstack', role: 'source', distribution: 'kolla', credentials_secret: 'provider-kolla-lab-b', status: 'ok' });
    expect(created?.credentials_updated_at).not.toBeNull();
    expect(JSON.stringify(server.providers)).not.toContain('Hunter2-secret');
    const event = server.events.find((e) => e.kind === 'provider.credentials_updated');
    expect(event?.data).toMatchObject({ provider_id: 'kolla-lab-b', keys: ['password', 'project_name', 'username'] });
    expect(JSON.stringify(event)).not.toContain('Hunter2-secret');
    await user.click(within(result).getByRole('button', { name: /^done$/i }));
    expect(await screen.findByRole('article', { name: 'Kolla Lab B' })).toBeInTheDocument();
  });

  it('connects vCenter with an existing conversion host and a key file that is never displayed', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    expect(within(dialog).queryByRole('radiogroup', { name: /use this cloud as/i })).not.toBeInTheDocument();
    expect(within(dialog).getByText(/source: vms are migrated from this cloud/i)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC3');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc3.example.com/sdk');
    await user.type(within(dialog).getByLabelText(/vcenter user/i), 'svc@vsphere.local');
    await user.type(within(dialog).getByLabelText(/^password/i), 'pw');
    await user.type(within(dialog).getByLabelText(/^datacenter$/i), 'DC3');
    // the conversion host is required for VMware: saving without it opens the section
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/ssh private key/i);
    await user.type(within(dialog).getByLabelText(/^address/i), '198.51.100.77');
    await user.upload(within(dialog).getByLabelText(/choose key file/i), new File([KEY], 'id_ed25519', { type: 'text/plain' }));
    expect(await within(dialog).findByText(/id_ed25519 .* ready to store/i)).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent('b3BlbnNzaC1rZXktdjEAAAAA');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const created = server.providers.find((p) => p.id === 'vcenter-dc3');
    expect(created).toMatchObject({ kind: 'vmware', role: 'source', distribution: 'vmware', credentials_secret: 'provider-vcenter-dc3' });
    expect(created?.conversion_host).toMatchObject({ manage: false, address: '198.51.100.77', ssh_key_secret: 'provider-vcenter-dc3-ssh' });
    expect(created?.conversion_key_updated_at).not.toBeNull();
    expect(await screen.findByRole('status')).toHaveTextContent(/vcenter dc3 added/i);
  });

  it('opens the closed conversion-host section when a summary line leads to one of its fields', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    const summary = await within(dialog).findByRole('alert');
    // the save opened the section for its errors; close it again
    await user.click(within(dialog).getByRole('button', { name: /existing host/i }));
    expect(within(dialog).queryByLabelText(/^address/i)).not.toBeInTheDocument();
    await user.click(within(summary).getByRole('link', { name: /ssh private key/i }));
    await waitFor(() => expect(dialog.querySelector('#provider-dialog-privateKey')).toHaveFocus());
    expect(within(dialog).getByLabelText(/^address/i)).toBeInTheDocument();
  });

  it('connects vCenter with secrets managed outside Seamless: it names them and writes none (SDD §13.3, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC4');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc4.example.com/sdk');
    await user.click(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' }));
    expect(within(dialog).queryByLabelText(/vcenter user/i)).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^secret name/i), 'vcenter-dc4');
    expect(within(dialog).getByRole('region', { name: 'Sign-in' })).toHaveTextContent(/managed outside Seamless, at each connection\. Seamless never writes/);
    expect(within(dialog).getByRole('region', { name: 'Sign-in' })).not.toHaveTextContent(/Stored in the platform/);
    await user.click(within(dialog).getByRole('button', { name: /existing host/i }));
    await user.type(within(dialog).getByLabelText(/^address/i), '198.51.100.78');
    await user.click(within(dialog).getByRole('radio', { name: 'Key in a secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^key secret name/i), 'conv-dc4-key');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const created = server.providers.find((p) => p.id === 'vcenter-dc4');
    expect(created).toMatchObject({ credentials_secret: 'vcenter-dc4', credentials_updated_at: null, conversion_key_updated_at: null });
    expect(created?.conversion_host).toMatchObject({ manage: false, address: '198.51.100.78', ssh_key_secret: 'conv-dc4-key' });
    expect(server.events.some((e) => e.kind === 'provider.credentials_updated' || e.kind === 'provider.conversion_key_updated')).toBe(false);
  });

  it('refuses a secret name the control plane could not read, before anything is sent', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC5');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc5.example.com/sdk');
    await user.click(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' }));
    await user.type(within(dialog).getByLabelText(/^secret name/i), '../etc');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/secret name/i);
    expect(within(dialog).getByLabelText(/^secret name/i)).toHaveAccessibleDescription(/letters, digits/i);
    expect(server.providers.some((p) => p.id === 'vcenter-dc5')).toBe(false);
  });

  it('refuses another provider’s secret, which a check would send to this provider’s endpoint (SDD §13.3)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'vCenter DC6');
    await user.type(within(dialog).getByLabelText(/vcenter url/i), 'https://vcenter.dc6.example.com/sdk');
    await user.click(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' }));
    // the hint says what an OpenShift Secret read through the API needs
    expect(within(dialog).getByLabelText(/^secret name/i)).toHaveAccessibleDescription(/seamless\.io\/provider-secret: "true"/);
    await user.type(within(dialog).getByLabelText(/^secret name/i), 'provider-community-lab');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/another provider/i);
    expect(server.providers.some((p) => p.id === 'vcenter-dc6')).toBe(false);
  });

  it('opens a provider that names a secret managed outside Seamless in that sign-in method', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const lab = server.providers.find((p) => p.id === 'community-lab')!;
    lab.cloud = null;
    lab.credentials_secret = 'lab-openstack';
    renderPage('admin', server);
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    expect(within(dialog).getByRole('radio', { name: 'Secret managed outside Seamless' })).toBeChecked();
    expect(within(dialog).getByLabelText(/^secret name/i)).toHaveValue('lab-openstack');
  });

  it('edits a provider: credentials stay empty, only changed fields are sent, the platform type is locked', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    expect(within(dialog).getByRole('radio', { name: /vmware vcenter/i })).toHaveAttribute('aria-disabled', 'true');
    expect(within(dialog).getByLabelText(/^id/i)).toBeDisabled();
    expect(within(dialog).getByRole('radio', { name: /clouds\.yaml entry/i })).toBeChecked();
    const region = within(dialog).getByLabelText(/^region/i);
    await user.clear(region);
    await user.type(region, 'RegionTwo');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const updated = server.events.filter((e) => e.kind === 'provider.updated').at(-1);
    expect(updated?.data).toEqual({ provider_id: 'community-lab', fields: ['region'] });
    expect(server.events.some((e) => e.kind === 'provider.credentials_updated')).toBe(false);
    expect(server.providers.find((p) => p.id === 'community-lab')?.region).toBe('RegionTwo');
  });

  it('says why another platform cannot be chosen when editing a provider: focusable, with the reason (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage();
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    const current = within(dialog).getAllByRole('radio').find((r) => (r as HTMLInputElement).checked)!;
    const vmware = within(dialog).getByRole('radio', { name: /vmware vcenter/i });
    // focusable and described, never natively disabled (SDD §16)
    expect(vmware).not.toBeDisabled();
    expect(vmware).toHaveAttribute('aria-disabled', 'true');
    expect(vmware).toHaveAccessibleDescription('A provider keeps its platform type; register a new provider instead.');
    expect(within(dialog).queryByRole('note')).not.toBeInTheDocument();

    // tapping or clicking it says why in a note under the platforms, and changes nothing
    await user.click(vmware);
    expect(within(dialog).getByRole('note')).toHaveTextContent('A provider keeps its platform type; register a new provider instead.');
    expect(vmware).not.toBeChecked();
    expect(current).toBeChecked();
  });

  it('keeps the dialog open with the reason when a running plan locks the provider', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const running = server.plans.find((p) => p.status === 'running');
    if (!running) throw new Error('fixture without a running plan');
    const locked = server.providers.find((p) => p.id === running.source_provider_id);
    renderPage('admin', server);
    const card = await screen.findByRole('article', { name: locked?.name });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/^name/i), ' (renamed)');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    expect(await within(dialog).findByText(/not all changes were saved/i)).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/running or paused plan/i);
  });

  it('keeps a provider whose credentials failed and saves them on the next try, without adding it again (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    // the credentials step fails once (e.g. the secret store is down)
    const target = server as unknown as { setCredentials: (...args: unknown[]) => unknown };
    const setCredentials = target.setCredentials.bind(server);
    let failures = 1;
    target.setCredentials = (...args: unknown[]) => {
      if (failures > 0) {
        failures -= 1;
        throw new Error('secret store unavailable');
      }
      return setCredentials(...args);
    };
    renderPage('admin', server);
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla Lab C');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-c.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /^add provider$/i }));

    // the provider was added; its credentials were not
    expect(await within(dialog).findByText(/internal error/i)).toBeInTheDocument();
    const added = () => server.providers.filter((p) => p.id === 'kolla-lab-c');
    expect(added()).toHaveLength(1);
    expect(added()[0]?.credentials_updated_at).toBeNull();
    // the dialog goes on editing it: the ID is fixed now, and Save sends what is missing
    expect(within(dialog).getByLabelText(/^id/i)).toBeDisabled();
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(added()[0]?.credentials_updated_at).not.toBeNull());
    expect(added()).toHaveLength(1);
    expect(JSON.stringify(server.providers)).not.toContain('Hunter2-secret');
  });

  it('scrolls a failed save into view: the long form would keep it out of sight (SDD §16)', async () => {
    const recorder = recordScrolls();
    try {
      const user = userEvent.setup({ delay: null });
      const server = createTestServer();
      // the credentials step fails (e.g. the secret store cannot be written)
      const target = server as unknown as { setCredentials: (...args: unknown[]) => unknown };
      target.setCredentials = () => {
        throw new Error('secret store unavailable');
      };
      renderPage('admin', server);
      const dialog = await openAdd(user);
      await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
      await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla Lab D');
      await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://kolla-d.example.com:5000/v3');
      await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
      await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
      await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
      await user.click(within(dialog).getByRole('button', { name: /^add and test connection$/i }));
      const alert = await within(dialog).findByRole('alert');
      expect(alert).toHaveTextContent(/not all changes were saved/i);
      expect(recorder.scrolls.map((s) => s.element)).toContain(alert);
    } finally {
      recorder.restore();
    }
  });

  it('after a connection test that did not pass, Edit again fixes the added provider instead of adding it again (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla Lab D');
    // the mock fails the connection test of an endpoint whose host starts with "unreachable"
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://unreachable.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));

    const failed = await screen.findByRole('dialog', { name: /kolla lab d was saved, but the connection test did not pass/i });
    await user.click(within(failed).getByRole('button', { name: /^edit again$/i }));
    const form = await screen.findByRole('dialog', { name: /edit kolla lab d/i });
    const url = within(form).getByLabelText(/keystone url/i);
    await user.clear(url);
    await user.type(url, 'https://kolla-d.example.com:5000/v3');
    await user.click(within(form).getByRole('button', { name: /save and test connection/i }));

    expect(await screen.findByRole('dialog', { name: /kolla lab d is connected/i })).toBeInTheDocument();
    const added = server.providers.filter((p) => p.id === 'kolla-lab-d');
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({ endpoint: 'https://kolla-d.example.com:5000/v3', status: 'ok' });
  });

  it('keeps the focus in the dialog when the test result replaces the form, and on Edit again (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('radio', { name: /kolla-ansible/i }));
    await user.type(within(dialog).getByLabelText(/^name/i), 'Kolla Lab F');
    await user.type(within(dialog).getByLabelText(/keystone url/i), 'https://unreachable.example.com:5000/v3');
    await user.type(within(dialog).getByLabelText(/^user name/i), 'svc-migrate');
    await user.type(within(dialog).getByLabelText(/^password/i), 'Hunter2-secret');
    await user.type(within(dialog).getByLabelText(/^project$/i), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));

    const failed = await screen.findByRole('dialog', { name: /kolla lab f was saved, but the connection test did not pass/i });
    // the result's heading takes the focus from the button that went away
    await waitFor(() => expect(within(failed).getByRole('heading', { level: 2 })).toHaveFocus());
    await user.click(within(failed).getByRole('button', { name: /^edit again$/i }));
    const form = await screen.findByRole('dialog', { name: /edit kolla lab f/i });
    await waitFor(() => expect(within(form).getByLabelText(/^name/i)).toHaveFocus());
  });

  it('names the plans that still need a provider when its deletion is refused (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('admin');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const card = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    await user.click(within(card).getByRole('button', { name: /^delete /i }));
    const dialog = await screen.findByRole('alertdialog', { name: /delete rhosp 17\.1/i });
    // what happens, row by row (SDD §16)
    expect(within(dialog).getByRole('list', { name: 'What happens' })).toHaveTextContent('Then plans that use it keep it until they finish and their VMs are finalized, as a rollback needs it');
    await user.type(within(dialog).getByLabelText(/type rhosp17-dc1 to confirm/i), 'rhosp17-dc1');
    await user.click(within(dialog).getByRole('button', { name: /^delete provider$/i }));
    const refusal = await within(dialog).findByRole('alert');
    expect(refusal).toHaveTextContent('plan-4f2a9c1e ("DC1 → RHOSO production rollout", running)');
    expect(within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' })).toBeInTheDocument();
  });

  it('moves the focus to the column heading once a deleted provider is gone (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('admin');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const card = within(from).getByRole('article', { name: 'Kolla Edge (Bobcat)' });
    await user.click(within(card).getByRole('button', { name: /^delete /i }));
    const dialog = await screen.findByRole('alertdialog', { name: /delete kolla edge/i });
    await user.type(within(dialog).getByLabelText(/type kolla-edge to confirm/i), 'kolla-edge');
    await user.click(within(dialog).getByRole('button', { name: /^delete provider$/i }));
    await waitFor(() => expect(within(from).queryByRole('article', { name: 'Kolla Edge (Bobcat)' })).not.toBeInTheDocument());
    await waitFor(() => expect(within(from).getByRole('heading', { name: 'Migrate from' })).toHaveFocus());
  });
});
