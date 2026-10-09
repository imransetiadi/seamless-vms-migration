import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import Providers from './Providers';

// a key-shaped string without key material (write-only upload fixture)
const KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n-----END OPENSSH PRIVATE KEY-----\n'; // gitleaks:allow

function renderPage(token = 'admin', server = createTestServer()) {
  return renderWithApp(<Providers />, { route: '/providers', path: '/providers', token, server });
}

async function openAdd(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: /^add provider$/i }));
  return screen.findByRole('dialog', { name: /connect a cloud/i });
}

describe('Providers page', () => {
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
    expect(within(rhosp).getByRole('button', { name: /^edit$/i })).toHaveAttribute('aria-disabled', 'true');
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
    const user = userEvent.setup();
    const { server } = renderPage('operator');
    const before = server.events.filter((e) => e.kind === 'provider.checked').length;
    await user.click(await screen.findByRole('button', { name: /check all/i }));
    await waitFor(() => expect(screen.getByText(/checked 6 providers/i)).toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.checked')).toHaveLength(before + 6);
  });

  it('summarises what is missing and focuses the summary', async () => {
    const user = userEvent.setup();
    renderPage();
    const dialog = await openAdd(user);
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/fields need attention/i);
    for (const text of [/recognise/i, /full url/i, /user name/i, /password/i, /project the user signs in to/i]) {
      expect(summary).toHaveTextContent(text);
    }
    await waitFor(() => expect(summary).toHaveFocus());
  });

  it('connects a Kolla-Ansible source: the preset fills the form, credentials stay write-only', async () => {
    const user = userEvent.setup();
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
    const user = userEvent.setup();
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

  it('edits a provider: credentials stay empty, only changed fields are sent, the platform type is locked', async () => {
    const user = userEvent.setup();
    const { server } = renderPage();
    const card = await screen.findByRole('article', { name: 'OpenStack Lab (2023.1)' });
    await user.click(within(card).getByRole('button', { name: /^edit$/i }));
    const dialog = await screen.findByRole('dialog', { name: /edit openstack lab/i });
    expect(within(dialog).getByRole('radio', { name: /vmware vcenter/i })).toBeDisabled();
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

  it('keeps the dialog open with the reason when a running plan locks the provider', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    const running = server.plans.find((p) => p.status === 'running');
    if (!running) throw new Error('fixture without a running plan');
    const locked = server.providers.find((p) => p.id === running.source_provider_id);
    renderPage('admin', server);
    const card = await screen.findByRole('article', { name: locked?.name });
    await user.click(within(card).getByRole('button', { name: /^edit$/i }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/^name/i), ' (renamed)');
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    expect(await within(dialog).findByText(/not all changes were saved/i)).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/running or paused plan/i);
  });
});
