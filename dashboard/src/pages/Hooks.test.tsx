import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Layout } from '../components/Layout';
import { createTestClient, createTestServer, renderWithApp } from '../test/utils';
import Hooks from './Hooks';

const DRAIN = { name: 'drain-lb', kind: 'playbook', playbook: '/opt/hooks/drain-lb.yml', timeout_s: 120 };

describe('the hook catalog (SDD §16, §23.3, plan task N12a)', () => {
  it('lets only an admin register a hook', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    const view = renderWithApp(<Hooks />, { route: '/hooks', server, token: 'admin' });
    expect(await screen.findByText('No hooks registered.')).toBeInTheDocument();
    expect(screen.getByText('Technology Preview')).toBeInTheDocument();
    // a playbook by default: its path and a timeout
    expect(screen.getByLabelText('Kind')).toHaveValue('playbook');
    await user.type(screen.getByLabelText(/^Name/), 'drain-lb');
    await user.type(screen.getByLabelText(/^Playbook path/), '/opt/hooks/drain-lb.yml');
    await user.clear(screen.getByLabelText('Timeout (s)'));
    await user.type(screen.getByLabelText('Timeout (s)'), '120');
    await user.click(screen.getByRole('button', { name: 'Register hook' }));
    const catalog = await screen.findByRole('list', { name: 'Hook catalog' });
    expect(await within(catalog).findByText('drain-lb')).toBeInTheDocument();
    expect(within(catalog).getByText('/opt/hooks/drain-lb.yml')).toBeInTheDocument();
    expect(await createTestClient(server, 'viewer').get('/hooks')).toEqual([expect.objectContaining(DRAIN)]);
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveValue(''));

    // a refused registration keeps what was typed and shows the API's reason
    await user.selectOptions(screen.getByLabelText('Kind'), 'webhook');
    await user.type(screen.getByLabelText(/^Name/), 'notify');
    await user.type(screen.getByLabelText(/^URL/), 'http://hooks.example/notify');
    await user.type(screen.getByLabelText(/^Bearer token/), 'smg_notify-token');
    await user.click(screen.getByRole('button', { name: 'Register hook' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Value error, url: an https:// URL with a host');
    expect(screen.getByLabelText(/^URL/)).toHaveValue('http://hooks.example/notify');
    view.unmount();

    // any other role reads the catalog and is told why it cannot register one
    for (const token of ['viewer', 'operator', 'approver']) {
      const other = renderWithApp(<Hooks />, { route: '/hooks', server, token });
      expect(await screen.findByText('drain-lb')).toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent('Registering hooks requires the admin role.');
      expect(screen.queryByRole('button', { name: 'Register hook' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Delete/ })).not.toBeInTheDocument();
      await expect(createTestClient(server, token).post('/hooks', { ...DRAIN, name: 'other' })).rejects.toMatchObject({ status: 403 });
      other.unmount();
    }
    // every role finds the page in the navigation, after Plans
    renderWithApp(<Layout />, { token: 'viewer' });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const links = within(nav).getAllByRole('link').map((link) => link.textContent);
    expect(links.indexOf('Hooks')).toBe(links.indexOf('Plans') + 1);
  });

  it("edits a hook with its name and kind read only, and says why one in use cannot be deleted", async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    const admin = createTestClient(server, 'admin');
    await admin.post('/hooks', DRAIN);
    await admin.post('/hooks', { name: 'undrain-lb', kind: 'playbook', playbook: '/opt/hooks/undrain-lb.yml' });
    await admin.patch('/hooks/drain-lb', { revert: 'undrain-lb' });
    renderWithApp(<Hooks />, { route: '/hooks', server, token: 'admin' });
    const catalog = await screen.findByRole('list', { name: 'Hook catalog' });
    const drain = within(catalog).getByRole('listitem', { name: 'drain-lb' });
    expect(drain).toHaveTextContent('Reverted by undrain-lb');
    await user.click(within(drain).getByRole('button', { name: 'Edit drain-lb' }));
    // Edit moves the focus to the name in the next frame: typed before that, the timeout's keys would follow it there
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    expect(screen.getByLabelText(/^Name/)).toHaveAttribute('readonly');
    expect(screen.getByLabelText('Kind')).toBeDisabled();
    await user.clear(screen.getByLabelText('Timeout (s)'));
    await user.type(screen.getByLabelText('Timeout (s)'), '90');
    await user.click(screen.getByRole('button', { name: 'Save hook' }));
    await waitFor(async () => expect(await admin.get('/hooks/drain-lb')).toMatchObject({ timeout_s: 90, revert: 'undrain-lb' }));
    // undrain-lb is drain-lb's revert: the API refuses, and its reason is shown
    const undrain = within(catalog).getByRole('listitem', { name: 'undrain-lb' });
    await user.click(within(undrain).getByRole('button', { name: 'Delete undrain-lb' }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete hook' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('hook undrain-lb is the revert of hook(s) drain-lb: change their revert first');
  });

  it('keeps a webhook\'s bearer token write-only and shows its URL to an admin alone (SDD §12, §13.3)', async () => {
    const user = userEvent.setup();
    const server = createTestServer();
    const admin = createTestClient(server, 'admin');
    const view = renderWithApp(<Hooks />, { route: '/hooks', server, token: 'admin' });
    await screen.findByText('No hooks registered.');
    await user.selectOptions(screen.getByLabelText('Kind'), 'webhook');
    await user.type(screen.getByLabelText(/^Name/), 'dns-swap');
    await user.type(screen.getByLabelText(/^URL/), 'https://hooks.example/in/PATH-TOKEN');
    const token = screen.getByLabelText(/^Bearer token/);
    expect(token).toHaveAttribute('type', 'password');
    await user.type(token, 'smg_canary-token');
    await user.click(screen.getByRole('button', { name: 'Register hook' }));
    const catalog = await screen.findByRole('list', { name: 'Hook catalog' });
    const row = await within(catalog).findByRole('listitem', { name: 'dns-swap' });
    expect(row).toHaveTextContent('https://hooks.example/in/PATH-TOKEN with a bearer token set');
    // no answer holds the token, for any role
    const stored = await admin.get<Record<string, unknown>>('/hooks/dns-swap');
    expect(stored).toMatchObject({ secret_set: true, url: 'https://hooks.example/in/PATH-TOKEN', url_host: 'hooks.example' });
    expect(JSON.stringify(stored)).not.toContain('smg_canary-token');
    expect(stored).not.toHaveProperty('secret');
    // an edit leaves the token field empty and keeps the one set
    await user.click(within(row).getByRole('button', { name: 'Edit dns-swap' }));
    await waitFor(() => expect(screen.getByLabelText(/^Name/)).toHaveFocus());
    expect(screen.getByLabelText(/^Bearer token/)).toHaveValue('');
    expect(screen.getByText(/A token is set\. Leave empty to keep it/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save hook' }));
    await waitFor(async () => expect((await admin.get<{ secret_updated_at: string }>('/hooks/dns-swap')).secret_updated_at).toBe(stored.secret_updated_at));
    // PUT /hooks/{name}/secret rotates it, an admin alone
    await expect(createTestClient(server, 'operator').put('/hooks/dns-swap/secret', { token: 'x' })).rejects.toMatchObject({ status: 403 });
    await expect(admin.put('/hooks/dns-swap/secret', { token: 'a b' })).rejects.toMatchObject({ status: 422 });
    expect(await admin.put('/hooks/dns-swap/secret', { token: 'rotated' })).toMatchObject({ secret_set: true });
    view.unmount();
    // any other role reads the receiver's host, never the URL
    renderWithApp(<Hooks />, { route: '/hooks', server, token: 'viewer' });
    const other = await screen.findByRole('listitem', { name: 'dns-swap' });
    expect(other).toHaveTextContent('Receiver hooks.example with a bearer token set');
    expect(other).not.toHaveTextContent('PATH-TOKEN');
    expect(await createTestClient(server, 'viewer').get('/hooks/dns-swap')).toMatchObject({ url: null, url_host: 'hooks.example' });
  });
});
