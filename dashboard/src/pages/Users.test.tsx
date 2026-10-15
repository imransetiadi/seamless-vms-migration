import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from '../App';
import { queryKeys } from '../api/hooks';
import { Layout } from '../components/Layout';
import { createTestClient, createTestServer, renderWithApp } from '../test/utils';
import Users from './Users';

function renderUsers(token = 'admin') {
  const server = createTestServer();
  server.health.auth_disabled = false;
  return renderWithApp(<Users />, { route: '/users', server, token });
}

afterEach(() => vi.restoreAllMocks());

describe('dashboard user access', () => {
  it('defaults to viewer, creates the selected role, and signs in with its one-time token', async () => {
    const user = userEvent.setup();
    const view = renderUsers();
    await screen.findByText('rina');
    expect(screen.getByLabelText('Role')).toHaveValue('viewer');
    await user.type(screen.getByLabelText(/user name/i), 'new.operator');
    await user.selectOptions(screen.getByLabelText('Role'), 'operator');
    await user.click(screen.getByRole('button', { name: 'Add user' }));
    const tokenField = await screen.findByLabelText('New user API token');
    expect(tokenField).toHaveFocus();
    const token = (tokenField as HTMLTextAreaElement).value;
    const identity = { name: 'new.operator', role: 'operator' };
    expect(await createTestClient(view.server, token).get('/me')).toEqual(identity);
    await waitFor(() => expect(view.queryClient.getQueryData(queryKeys.users)).toContainEqual(identity));
    expect(JSON.stringify(view.queryClient.getQueryData(queryKeys.users))).not.toContain(token);
    await user.click(screen.getByRole('button', { name: 'Copy token' }));
    expect(await screen.findByText('Token copied.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByLabelText('New user API token')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText(/user name/i)).toHaveFocus());
    expect(screen.getByLabelText(/user name/i)).toHaveValue('');
    await waitFor(() => expect(view.queryClient.getMutationCache().getAll()).toHaveLength(0));
    view.unmount();
    const login = renderWithApp(<AppRoutes />, { route: '/login', server: view.server, storedToken: true });
    await user.type(await screen.findByLabelText('API token'), token);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('heading', { name: 'Overview', level: 1 });
    expect(login.queryClient.getQueryData(queryKeys.me)).toEqual(identity);
  });

  it('keeps token selectable when clipboard access fails', async () => {
    const user = userEvent.setup();
    renderUsers();
    await screen.findByText('rina');
    await user.type(screen.getByLabelText(/user name/i), 'alice');
    await user.click(screen.getByRole('button', { name: 'Add user' }));
    await screen.findByLabelText('New user API token');
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('blocked'));
    await user.click(screen.getByRole('button', { name: 'Copy token' }));
    expect(await screen.findByText('Copy failed. Select the token and copy it manually.')).toBeInTheDocument();
    expect(screen.getByLabelText('New user API token')).toHaveAttribute('readonly');
  });

  it('keeps input and shows duplicate errors without issuing another token', async () => {
    const user = userEvent.setup();
    renderUsers();
    await screen.findByText('rina');
    await user.type(screen.getByLabelText(/user name/i), 'RINA');
    await user.click(screen.getByRole('button', { name: 'Add user' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('a user with this name already exists');
    expect(screen.getByLabelText(/user name/i)).toHaveValue('RINA');
    expect(screen.queryByLabelText('New user API token')).not.toBeInTheDocument();
  });

  it.each(['viewer', 'operator', 'approver'])('refuses %s access to the form and API', async (token) => {
    const view = renderUsers(token);
    expect(screen.getByRole('status')).toHaveTextContent('requires the admin role');
    expect(screen.queryByLabelText(/user name/i)).not.toBeInTheDocument();
    const client = createTestClient(view.server, token);
    await expect(client.get('/users')).rejects.toMatchObject({ status: 403 });
    await expect(client.post('/users', { name: 'alice' })).rejects.toMatchObject({ status: 403 });
    view.unmount();
    renderWithApp(<Layout />, { token });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    expect(within(nav).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
  });

  it('explains why auth-disabled deployments cannot add users', async () => {
    renderWithApp(<Users />);
    await screen.findByText('rina');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add user' })).toHaveAccessibleDescription('Enable authentication on the control plane before adding users.'));
  });

  it('retries a failed user list', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    const fail = vi.spyOn(server, 'handle').mockImplementation((method, path, ...args) =>
      path === '/users' && method === 'GET' ? { status: 503, body: { error: { code: 'unavailable', message: 'Try again' } } } : handle(method, path, ...args),
    );
    const user = userEvent.setup();
    renderWithApp(<Users />, { server });
    await screen.findByRole('alert');
    fail.mockRestore();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('rina')).toBeInTheDocument();
  });
});
