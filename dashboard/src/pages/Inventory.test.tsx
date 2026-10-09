import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import Inventory from './Inventory';

const REFUSED = { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } };

describe('Inventory', () => {
  it('names a failed provider load with Retry instead of showing a blank page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const handle = server.handle.bind(server);
    // a 4xx is not retried: the error shows at once
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/providers' ? REFUSED : handle(method, path, query, body, token);
    renderWithApp(<Inventory />, { route: '/inventory/rhosp17-dc1', path: '/inventory/:providerId', server });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/providers are unavailable/i);
    // Retry brings the inventory back once the providers load
    server.handle = handle;
    await user.click(within(alert).getByRole('button', { name: /retry/i }));
    expect(await screen.findByRole('table', { name: /vms on/i })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows Create plan to viewers of a source as unavailable with the reason (SDD §16)', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhosp17-dc1', path: '/inventory/:providerId', token: 'viewer' });
    await screen.findByRole('table', { name: /vms on/i });
    const create = screen.getByRole('button', { name: /^create plan/i });
    expect(create).toHaveAttribute('aria-disabled', 'true');
    expect(create).toHaveAccessibleDescription('Creating a plan requires the operator role.');
    // viewers cannot select VMs, so the page does not ask them to
    expect(screen.queryByText(/select vms below/i)).not.toBeInTheDocument();
  });

  it('offers no Create plan on a destination inventory', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhoso-prod', path: '/inventory/:providerId', token: 'operator' });
    await screen.findByRole('heading', { level: 1, name: 'Inventory' });
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /^create plan/i })).not.toBeInTheDocument();
  });

  it('shows that it is loading, not a blank page, while the providers load', async () => {
    const pending = new Promise<Response>(() => {});
    renderWithApp(<Inventory />, {
      route: '/inventory/rhosp17-dc1',
      path: '/inventory/:providerId',
      wrapFetch: (mock) => (input, init) => (String(input).split('?')[0]!.endsWith('/api/v1/providers') ? pending : mock(input, init)),
    });
    expect(await screen.findByRole('status')).toHaveTextContent(/reading inventory/i);
  });
});
