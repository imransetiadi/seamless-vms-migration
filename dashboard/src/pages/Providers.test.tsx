import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { DestinationInventory, GpuCapacity } from '../api/types';
import { TECH_PREVIEW_DETAIL } from '../components/TechPreviewBadge';
import { formatDateTime } from '../lib/format';
import { destinationRelease } from '../lib/platforms';
import { createTestServer, recordScrolls, renderWithApp, TEST_NOW } from '../test/utils';
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
    // a community OpenStack destination beside RHOSO 18.0, as in the API's demo (SDD §10, plan T9)
    expect(within(to).getByRole('heading', { name: 'OpenStack 2026.2 Hibiscus (DC3)' })).toBeInTheDocument();
    const rhosp = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    expect(rhosp).toHaveTextContent('Red Hat OpenStack 17.1');
    expect(rhosp).toHaveTextContent(/stored .* ago/i);
    expect(within(from).getByRole('article', { name: 'Kolla Edge (Bobcat)' })).toHaveTextContent(/not set\. edit the provider to add sign-in details/i);
    // the fleet summary counts every provider by status and those without sign-in details
    const summary = screen.getByRole('list', { name: /providers by status/i });
    expect(summary).toHaveTextContent(/healthy\s*3/i);
    expect(screen.getByText(/1 without sign-in details/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check all/i })).toHaveAttribute('aria-disabled', 'true');
    // viewers learn the actions exist but cannot use them
    expect(screen.getByRole('button', { name: /^add provider$/i })).toHaveAttribute('aria-disabled', 'true');
    expect(within(rhosp).getByRole('button', { name: /^edit /i })).toHaveAttribute('aria-disabled', 'true');
  });

  it('names RHOSO 18.0 and community OpenStack as destinations (SDD §16, plan T6)', async () => {
    renderPage('viewer');
    const header = await screen.findByRole('heading', { level: 1, name: 'Providers' });
    const description = header.parentElement!.textContent ?? '';
    expect(description).toContain('RHOSO 18.0 and community OpenStack 2023.1 Antelope – 2026.2 Hibiscus as destinations');
    expect(description).not.toContain('RHOSO 18.0 as the destination');
  });

  it('names the destinations in its empty state too (SDD §16, plan T6)', async () => {
    const server = createTestServer();
    server.providers.splice(0);
    renderPage('admin', server);
    expect(await screen.findByText('Connect your first cloud')).toBeInTheDocument();
    expect(screen.getByText(/^Add the source you migrate from/)).toHaveTextContent(
      'Add the source you migrate from and the destination you migrate to. Destinations are RHOSO 18.0 and community OpenStack 2023.1 Antelope – 2026.2 Hibiscus. Sign-in details go to the secret store, never to the database.',
    );
  });

  it("shows a destination's series, tier and policy profile", async () => {
    renderPage('viewer');
    const to = await screen.findByRole('region', { name: 'Migrate to' });
    const detail = (card: HTMLElement, term: string) => within(card).getByText(term, { selector: 'dt' }).nextElementSibling as HTMLElement;
    // a community destination: its declared series, the tier of that series and the profile it implies (SDD §10.1, §16)
    const hibiscus = within(to).getByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    expect(detail(hibiscus, 'Series')).toHaveTextContent(/^2026\.2 Hibiscus\s*declared$/);
    expect(detail(hibiscus, 'Support tier')).toHaveTextContent(/^Tier 1$/);
    expect(detail(hibiscus, 'Policy profile')).toHaveTextContent(/^Secure RBAC defaults$/);
    // RHOSO 18.0: validated and legacy by its kind; the range its APIs read (D14)
    const rhoso = within(to).getByRole('article', { name: 'RHOSO 18.0 — Production' });
    expect(detail(rhoso, 'Series')).toHaveTextContent(/^2023\.1 Antelope – 2023\.2 Bobcat\s*detected$/);
    expect(detail(rhoso, 'Support tier')).toHaveTextContent(/^Validated$/);
    expect(detail(rhoso, 'Policy profile')).toHaveTextContent(/^Legacy rules$/);
    // a source has no series to show
    expect(within(screen.getByRole('article', { name: 'RHOSP 17.1 — DC1' })).queryByText('Series', { selector: 'dt' })).toBeNull();
    // review RV18-08: every reported value is printed in words, never "[object Object]" or "null"
    for (const card of screen.getAllByRole('article')) expect(card.textContent).not.toMatch(/\[object Object\]|\bnull\b/);
  });

  it('warns of a series its APIs contradict, an unknown profile and endpoints on plain HTTP (SDD §16)', async () => {
    const server = createTestServer();
    const hibiscus = server.providers.find((p) => p.id === 'hibiscus-dc3')!;
    hibiscus.capabilities = {
      ...hibiscus.capabilities,
      release_range: ['2024.1', '2024.2'],
      release: null,
      support_tier: { tier: 'tier3', reason: 'end_of_life', declared_outside_range: true },
      policy_profile: 'unknown',
      tls: false,
      plain_http: ['auth_url', 'compute'],
    };
    renderPage('viewer', server);
    const card = await screen.findByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    const detail = (term: string) => within(card).getByText(term, { selector: 'dt' }).nextElementSibling as HTMLElement;
    expect(detail('Series')).toHaveTextContent('2024.1 Caracal – 2024.2 Dalmatian');
    expect(detail('Series')).toHaveTextContent(
      'Declared 2026.2 Hibiscus, which its APIs contradict: correct the declared series, or clear it so the check detects the series.',
    );
    expect(detail('Support tier')).toHaveTextContent('Tier 3 · End of Life upstream');
    // both ways out, as the API's refusals: a check is allowed while a plan runs, a PATCH that declares is not (SDD §16,
    // §21.5, review RV20-09)
    expect(detail('Policy profile')).toHaveTextContent(
      "Unknown. Check the provider so its series is detected, or declare its policy profile or series: until then only the admin role may put a port on another project's network.",
    );
    expect(detail('TLS')).toHaveTextContent('Plain HTTP: the auth URL and compute. Credentials travel unencrypted: use HTTPS outside a lab.');
  });

  it('says a maximum the check could not read "not read" and an empty list "none", never a bare label (SDD §16, review RV19-03)', async () => {
    const server = createTestServer();
    const hibiscus = server.providers.find((p) => p.id === 'hibiscus-dc3')!;
    // SDD §10: a maximum the check could not read is "" - DST_CAPABILITY_MISSING then judges nothing
    hibiscus.capabilities = { ...hibiscus.capabilities, compute_microversion: '', block_storage_microversion: '', roles: [] };
    renderPage('viewer', server);
    const card = await screen.findByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    const chip = (label: string) => within(within(card).getByRole('list', { name: 'Capabilities' })).getByText(label).closest('li');
    expect(chip('Compute API')).toHaveTextContent(/^Compute API\s*not read$/);
    expect(chip('Block storage API')).toHaveTextContent(/^Block storage API\s*not read$/);
    expect(chip('Token roles')).toHaveTextContent(/^Token roles\s*none$/);
    // a value the check read is said as it was read
    expect(chip('Storage')).toHaveTextContent(/^Storage\s*Ceph RBD/);
  });

  it('says why a read range is empty: older or newer than the window, "Not detected" only when nothing was read (SDD §16, review RV19-04)', async () => {
    const cases = [
      { id: 'hibiscus-dc3', kind: 'openstack', compute: '2.93', blockStorage: '3.70', series: 'Older than the window', tier: 'Outside · older than 2023.1' },
      { id: 'hibiscus-dc3', kind: 'openstack', compute: '2.105', blockStorage: '3.71', series: 'Newer than the window', tier: 'Outside · newer than 2026.2' },
      { id: 'hibiscus-dc3', kind: 'openstack', compute: '', blockStorage: '', series: 'Not detected', tier: 'Outside · series not detected' },
      // RHOSO 18.0 is validated by its kind: its maxima, not its tier, say why its range is empty
      { id: 'rhoso-prod', kind: 'rhoso', compute: '2.93', blockStorage: '3.70', series: 'Older than the window', tier: 'Validated' },
    ] as const;
    for (const c of cases) {
      const server = createTestServer();
      const provider = server.providers.find((p) => p.id === c.id)!;
      provider.release = null;
      provider.capabilities = {
        ...provider.capabilities,
        compute_microversion: c.compute,
        block_storage_microversion: c.blockStorage,
        ...destinationRelease(c.kind, null, null, c.compute, c.blockStorage),
      };
      const view = renderPage('viewer', server);
      const card = await screen.findByRole('article', { name: provider.name });
      const detail = (term: string) => within(card).getByText(term, { selector: 'dt' }).nextElementSibling as HTMLElement;
      expect(detail('Series')).toHaveTextContent(new RegExp(`^${c.series}$`));
      expect(detail('Support tier')).toHaveTextContent(c.tier);
      view.unmount();
    }
  });

  it("says a vCenter's check in words, with no OpenStack service (review RV19-05)", async () => {
    renderPage('viewer');
    const card = await screen.findByRole('article', { name: 'vCenter HQ' });
    const chips = within(card).getByRole('list', { name: 'Capabilities' });
    const chip = (label: string) => within(chips).getByText(label).closest('li');
    expect(chip('vSphere API')).toHaveTextContent(/^vSphere API\s*8\.0\.2\.0$/);
    expect(chip('VMs with CBT')).toHaveTextContent(/^VMs with CBT\s*\d+$/);
    expect(chip('VMs without VMware Tools')).toHaveTextContent(/^VMs without VMware Tools\s*\d+$/);
    expect(chips).not.toHaveTextContent(/Compute API|OVN networking|Volume backends|_/);
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
    const total = server.providers.length;
    await user.click(await screen.findByRole('button', { name: /check all/i }));
    await waitFor(() => expect(screen.getByText(new RegExp(`checked ${total} providers`, 'i'))).toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.checked')).toHaveLength(before + total);
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

  it('asks for a name when it has no visible character, as the API refuses it (SDD §4.2, review RV21-08)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage();
    const dialog = await openAdd(user);
    await user.type(within(dialog).getByLabelText(/^name/i), '\u200b');
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/recognise/i);
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
    // the API's check gives a provider that answers no status message (review RV22-03): the result says what it is
    expect(result).toHaveTextContent(/connection test result: /i);
    expect(result).not.toHaveTextContent(/did not pass/i);
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

  it('does not offer Delete while plans still need the provider, and says which (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('admin');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const card = within(from).getByRole('article', { name: 'RHOSP 17.1 — DC1' });
    const remove = within(card).getByRole('button', { name: /^delete /i });
    await waitFor(() => expect(remove).toHaveAttribute('aria-disabled', 'true'));
    // as the API refuses it: the plans neither completed nor cancelled, a completed one whose VMs are not finalized
    expect(remove).toHaveAccessibleDescription(
      'Plans still need it: “DC1 → RHOSO production rollout” (running), “Shared Ceph handover — analytics” (validated), “Pilot — static content” (1 VM not finalized) and 1 more. A plan needs its providers until it completes and its VMs are finalized.',
    );
    await user.click(remove);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('moves the focus to the column heading once a deleted provider is gone (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderPage('admin');
    const from = await screen.findByRole('region', { name: 'Migrate from' });
    const card = within(from).getByRole('article', { name: 'Kolla Edge (Bobcat)' });
    await user.click(within(card).getByRole('button', { name: /^delete /i }));
    const dialog = await screen.findByRole('alertdialog', { name: /delete kolla edge/i });
    // what happens, row by row (SDD §16): no plan needs it any more
    expect(within(dialog).getByRole('list', { name: 'What happens' })).toHaveTextContent('Then finished plans keep its id: none of them needs it any more');
    await user.type(within(dialog).getByLabelText(/type kolla-edge to confirm/i), 'kolla-edge');
    await user.click(within(dialog).getByRole('button', { name: /^delete provider$/i }));
    await waitFor(() => expect(within(from).queryByRole('article', { name: 'Kolla Edge (Bobcat)' })).not.toBeInTheDocument());
    await waitFor(() => expect(within(from).getByRole('heading', { name: 'Migrate from' })).toHaveFocus());
  });
});

describe("Providers: a destination's GPU capacity (SDD §16, §22.11; plan task G11)", () => {
  /** A card's GPU capacity, named by its heading. */
  const gpuOf = (card: HTMLElement) => within(card).getByRole('group', { name: 'GPU capacity' });
  const lines = (group: HTMLElement) => within(group).getAllByRole('listitem').map((item) => item.textContent);
  const SEEDED = ['VGPU, CUSTOM_NVIDIA_A100_4C: 4 free on 1 GPU', 'VGPU, CUSTOM_NVIDIA_A100_3_20C: 2 free on 1 GPU', 'VGPU: 6 free on 2 GPUs, at most 4 on one'];

  it("summarizes a destination's GPU capacity with the check's time", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    renderPage('operator', server);
    // as each check read it: every pool by its class and traits, then its free units and GPUs, as the Inventory words them
    for (const id of ['rhoso-prod', 'hibiscus-dc3']) {
      const provider = server.providers.find((p) => p.id === id)!;
      const gpu = gpuOf(await screen.findByRole('article', { name: provider.name }));
      expect(within(gpu).getByRole('button', { name: 'Technology Preview' })).toHaveAccessibleDescription(TECH_PREVIEW_DETAIL);
      expect(lines(gpu)).toEqual(SEEDED);
      // with the check's time: the read's own
      const time = within(gpu).getByText(/^Checked$/).querySelector('time');
      expect(time).toHaveAttribute('datetime', provider.last_checked_at);
      expect(time).toHaveAttribute('title', formatDateTime(provider.last_checked_at));
    }
    // a vGPU taken since then: the card keeps the check's figures until a check reads them again
    const inventory = server.inventories['rhoso-prod'] as DestinationInventory;
    inventory.gpu_capacity!.pools = inventory.gpu_capacity!.pools.map((pool) =>
      pool.traits.includes('CUSTOM_NVIDIA_A100_4C') ? { ...pool, free: 3, largest_free: 3 } : pool.traits.length ? pool : { ...pool, free: 5, largest_free: 3 },
    );
    const card = screen.getByRole('article', { name: 'RHOSO 18.0 — Production' });
    expect(lines(gpuOf(card))).toEqual(SEEDED);
    await user.click(within(card).getByRole('button', { name: /^check /i }));
    await waitFor(() => expect(within(gpuOf(card)).getByText(/^Checked$/).querySelector('time')).toHaveAttribute('datetime', new Date(TEST_NOW).toISOString()));
    expect(lines(gpuOf(card))).toEqual(['VGPU, CUSTOM_NVIDIA_A100_4C: 3 free on 1 GPU', 'VGPU, CUSTOM_NVIDIA_A100_3_20C: 2 free on 1 GPU', 'VGPU: 5 free on 2 GPUs, at most 3 on one']);
  });

  it("says why a destination's GPU capacity was not read", async () => {
    const failures: Array<[Pick<GpuCapacity, 'reason' | 'detail'>, string]> = [
      [{ reason: 'forbidden', detail: 'HTTP 403' }, 'Not read: the destination credential lacks the role Placement asks for, admin by default (HTTP 403).'],
      [{ reason: 'no_placement', detail: null }, "Not read: the destination's catalog lists no Placement."],
      [{ reason: 'error', detail: 'HTTP 503' }, 'Not read: the read failed (HTTP 503).'],
    ];
    for (const [failure, said] of failures) {
      const server = createTestServer();
      const hibiscus = server.providers.find((p) => p.id === 'hibiscus-dc3')!;
      // the read's own time: the API's check reads Placement before it stamps last_checked_at
      const readAt = new Date(Date.parse(hibiscus.last_checked_at!) - 1500).toISOString();
      hibiscus.capabilities = { ...hibiscus.capabilities, gpu_capacity: { status: 'unknown', ...failure, read_at: readAt, pools: [] } };
      const view = renderPage('viewer', server);
      const gpu = gpuOf(await screen.findByRole('article', { name: hibiscus.name }));
      expect(gpu, failure.reason ?? '').toHaveTextContent(said);
      // nothing read is never shown as free units; the badge and the check's time stay
      expect(within(gpu).queryByRole('list')).not.toBeInTheDocument();
      expect(gpu).not.toHaveTextContent(/free on|None free/);
      expect(within(gpu).getByRole('button', { name: 'Technology Preview' })).toBeInTheDocument();
      expect(within(gpu).getByText(/^Checked$/).querySelector('time')).toHaveAttribute('datetime', readAt);
      view.unmount();
    }
    // a check whose read Placement refuses says so on the card
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    (server.inventories['rhoso-prod'] as DestinationInventory).gpu_capacity = { status: 'unknown', reason: 'forbidden', detail: 'HTTP 403', read_at: null, pools: [] };
    renderPage('operator', server);
    const card = await screen.findByRole('article', { name: 'RHOSO 18.0 — Production' });
    expect(lines(gpuOf(card))).toEqual(SEEDED);
    await user.click(within(card).getByRole('button', { name: /^check /i }));
    await waitFor(() => expect(gpuOf(card)).toHaveTextContent('Not read: the destination credential lacks the role Placement asks for, admin by default (HTTP 403).'));
    expect(within(gpuOf(card)).getByText(/^Checked$/).querySelector('time')).toHaveAttribute('datetime', new Date(TEST_NOW).toISOString());
  });

  it('shows no GPU line for a destination without GPU flavors', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    // a check report from before 0.3.0 holds no gpu_capacity
    const hibiscus = server.providers.find((p) => p.id === 'hibiscus-dc3')!;
    delete hibiscus.capabilities.gpu_capacity;
    renderPage('operator', server);
    const staging = await screen.findByRole('article', { name: 'RHOSO 18.0 — Staging' });
    // never checked: no report to summarize
    expect(within(staging).queryByRole('group', { name: 'GPU capacity' })).not.toBeInTheDocument();
    // checked, it lists no GPU flavor: its check reads ok with no pool and sends Placement nothing (§22.3)
    await user.click(within(staging).getByRole('button', { name: /^check /i }));
    expect(await screen.findByText(/^RHOSO 18\.0 — Staging checked: /)).toBeInTheDocument();
    expect(within(staging).getByText('Last checked', { selector: 'dt' }).nextElementSibling).not.toHaveTextContent('Never');
    expect(server.providers.find((p) => p.id === 'rhoso-staging')?.capabilities.gpu_capacity).toMatchObject({ status: 'ok', pools: [] });
    expect(within(staging).queryByRole('group', { name: 'GPU capacity' })).not.toBeInTheDocument();
    expect(staging).not.toHaveTextContent(/GPU|Technology Preview/);
    // a source has none, and only the GPU destination checked since 0.3.0 shows one
    for (const name of ['RHOSP 17.1 — DC1', 'vCenter HQ', 'OpenStack Lab (2023.1)', 'Kolla Edge (Bobcat)', hibiscus.name]) {
      expect(within(screen.getByRole('article', { name })).queryByRole('group', { name: 'GPU capacity' }), name).not.toBeInTheDocument();
    }
    expect(screen.getAllByRole('group', { name: 'GPU capacity' }).map((group) => group.closest('article'))).toEqual([screen.getByRole('article', { name: 'RHOSO 18.0 — Production' })]);
  });
});

describe("a source's Ceph access (SDD §16, §28.4, plan task CE10)", () => {
  // a key-shaped base64 string without key material (write-only, never displayed)
  const CEPH_KEY = 'AQBrK3NnRVhBTVBMRQAAAAAAAAAAAAAAAAAAAA=='; // gitleaks:allow
  const FSID = '0f1e2d3c-4b5a-4968-8777-665544332211';

  it("edits a source's Ceph access and writes its key once", async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderPage('admin');
    // community-lab: an OpenStack source of the seed without Ceph access
    const source = server.providers.find((p) => p.id === 'community-lab')!;
    expect([source.role, source.ceph ?? null]).toEqual(['source', null]);
    const card = await screen.findByRole('article', { name: source.name });
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog');
    const section = within(dialog).getByRole('region', { name: 'Ceph access' });
    expect(within(section).getByText('Technology Preview')).toBeInTheDocument();
    await user.click(within(section).getByRole('checkbox', { name: /declare the source cluster's ceph access/i }));
    await user.type(within(section).getByLabelText(/^cluster fsid/i), FSID);
    await user.type(within(section).getByLabelText(/^monitors/i), 'mon-a.example.com\nmon-b.example.com:6789');
    expect(within(section).getByLabelText(/^ceph user/i)).toHaveValue('seamless');
    await user.type(within(section).getByLabelText(/^ceph key/i), CEPH_KEY);
    expect(dialog).not.toHaveTextContent(CEPH_KEY);
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(source.ceph).toEqual({ fsid: FSID, monitors: ['mon-a.example.com', 'mon-b.example.com:6789'], client: 'seamless', key_updated_at: expect.any(String) });
    expect(JSON.stringify(server.providers)).not.toContain(CEPH_KEY);
    // one write of the key, named by its secret and key, never its value (SDD §28.8)
    const writes = server.events.filter((e) => e.kind === 'provider.credentials_updated');
    expect(writes).toHaveLength(1);
    expect(writes[0]!.data).toMatchObject({ provider_id: 'community-lab', secret: 'provider-community-lab.ceph', keys: ['key'] });
    expect(JSON.stringify(writes)).not.toContain(CEPH_KEY);
    expect(within(await screen.findByRole('article', { name: source.name })).getByRole('group', { name: 'Ceph access' })).toHaveTextContent(/Key stored/);
  });

  it('never shows a Ceph key again', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    // a source whose access and key are stored, and that no running plan holds (a PATCH of one is refused, SDD §12)
    const source = server.providers.find((p) => p.id === 'community-lab')!;
    source.ceph = { fsid: FSID, monitors: ['mon-a.example.com'], client: 'seamless', key_updated_at: '2026-10-01T08:00:00Z' };
    renderPage('admin', server);
    const card = await screen.findByRole('article', { name: source.name });
    const group = within(card).getByRole('group', { name: 'Ceph access' });
    expect(group).toHaveTextContent(/Key stored/);
    expect(within(group).getByText('Technology Preview')).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog');
    const section = within(dialog).getByRole('region', { name: 'Ceph access' });
    expect(within(section).getByRole('checkbox', { name: /declare the source cluster's ceph access/i })).toBeChecked();
    expect(within(section).getByText(/^A Ceph key stored .*never shown again\.$/)).toBeInTheDocument();
    expect(within(section).getByLabelText(/^ceph key/i)).toHaveValue('');
    expect(JSON.stringify(source)).not.toMatch(/"key":/);
    // saving without a key keeps the stored one: nothing is written, and its time stays
    await user.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.events.filter((e) => e.kind === 'provider.credentials_updated')).toHaveLength(0);
    expect(source.ceph?.key_updated_at).toBe('2026-10-01T08:00:00Z');
  });

  it("marks a pool whose fsid is not the provider's", async () => {
    const server = createTestServer();
    const rhosp = server.providers.find((p) => p.id === 'rhosp17-dc1')!;
    const backends = rhosp.capabilities.storage_backends as Array<Record<string, unknown>>;
    rhosp.capabilities = {
      ...rhosp.capabilities,
      storage_backends: [
        ...backends,
        { pool: 'overcloud@tripleo_ceph_dr#volumes-dr', vendor: 'Ceph', protocol: 'ceph', family: 'rbd', ceph_fsid: 'ffffffff-0000-4000-8000-000000000001', ceph_pool: 'volumes-dr' },
      ],
    };
    renderPage('admin', server);
    const card = await screen.findByRole('article', { name: 'RHOSP 17.1 — DC1' });
    expect(within(card).getByRole('group', { name: 'Ceph access' })).toHaveTextContent('On another cluster, untracked: overcloud@tripleo_ceph_dr#volumes-dr');
  });
});
