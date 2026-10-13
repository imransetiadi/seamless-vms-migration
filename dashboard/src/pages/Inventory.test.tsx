import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes, useNavigationType } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { Layout } from '../components/Layout';
import { createTestServer, renderWithApp } from '../test/utils';
import Inventory from './Inventory';

function NavigationType() {
  return <output data-testid="navigation-type">{useNavigationType()}</output>;
}

const REFUSED = { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } };

describe('Inventory', () => {
  it('keeps a failed inventory said, with Retry busy and focused, while it is read again (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    let hold: Promise<void> | null = null;
    let release: () => void = () => {};
    // the inventory read always fails (a 4xx, which the hook does not retry on its own); a hold keeps a retry running
    const wrapFetch = (fetchImpl: typeof fetch): typeof fetch => async (input, init) => {
      if (!String(input).includes('/providers/rhosp17-dc1/inventory')) return fetchImpl(input, init);
      if (hold) await hold;
      return new Response(JSON.stringify(REFUSED.body), { status: REFUSED.status, headers: { 'Content-Type': 'application/json' } });
    };
    renderWithApp(<Inventory />, { route: '/inventory/rhosp17-dc1', path: '/inventory/:providerId', token: 'viewer', wrapFetch });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/cannot read the inventory/i);
    hold = new Promise<void>((resolve) => (release = resolve));
    const retry = within(alert).getByRole('button', { name: /retry/i });
    await user.click(retry);
    // read again: the failure stays said, Retry is busy and keeps the focus, no loading placeholder
    await waitFor(() => expect(within(screen.getByRole('alert')).getByRole('button', { name: /retry/i })).toHaveAttribute('aria-busy', 'true'));
    expect(screen.getByRole('alert')).toHaveTextContent(/cannot read the inventory/i);
    expect(screen.queryByText(/reading inventory/i)).not.toBeInTheDocument();
    expect(within(screen.getByRole('alert')).getByRole('button', { name: /retry/i })).toHaveFocus();
    release();
    // it failed again: Retry is ready for another try, still focused
    await waitFor(() => expect(within(screen.getByRole('alert')).getByRole('button', { name: /retry/i })).not.toHaveAttribute('aria-busy'));
    expect(within(screen.getByRole('alert')).getByRole('button', { name: /retry/i })).toHaveFocus();
  });

  it('opens searching for the VM a link names by its source id (SDD §16, ADV-02)', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhosp17-dc1?vm=os-0a14', path: '/inventory/:providerId', token: 'viewer' });
    const search = await screen.findByLabelText('Search VMs');
    await waitFor(() => expect(search).toHaveValue('web-cache-01'));
    const table = screen.getByRole('table', { name: /VMs on/ });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('web-cache-01');
  });

  it('keeps focus on the Provider select while it switches the inventory, and replaces the address (WCAG 3.2.2, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(
      <Routes>
        <Route
          element={
            <>
              <Layout />
              <NavigationType />
            </>
          }
        >
          <Route path="/inventory/:providerId" element={<Inventory />} />
          <Route path="/events" element={<p>Events</p>} />
        </Route>
      </Routes>,
      { route: '/inventory/rhosp17-dc1' },
    );
    const select = await screen.findByRole('combobox', { name: 'Provider' });
    await screen.findByRole('table', { name: /^VMs on RHOSP 17.1 — DC1/ });
    select.focus();
    await user.selectOptions(select, 'vcenter-hq');
    await screen.findByRole('table', { name: /^VMs on vCenter HQ/ });
    expect(select).toHaveFocus();
    expect(screen.getByTestId('navigation-type')).toHaveTextContent('REPLACE');
    // any other navigation still moves focus to the page's main region, for screen readers to read the new page
    await user.click(screen.getAllByRole('link', { name: 'Events' })[0]!);
    expect(screen.getByRole('main')).toHaveFocus();
  });

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

  it('names the plan that holds a VM, and lists the free ones with the Plan filter (SDD §5.4, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const releases = new Set(['cancelled', 'finalized', 'rolled_back']);
    const plan = server.plans.find((p) => p.source_provider_id === 'rhosp17-dc1')!;
    const holding = server.migrations.find((m) => m.plan_id === plan.id && !releases.has(m.phase))!;
    renderWithApp(<Inventory />, { route: '/inventory/rhosp17-dc1', path: '/inventory/:providerId', token: 'operator', server });
    const table = await screen.findByRole('table', { name: /VMs on/ });
    const rowOf = (name: string) => within(table).queryAllByRole('row').find((r) => within(r).queryByRole('rowheader')?.textContent === name);
    await waitFor(() => expect(within(rowOf(holding.vm.name)!).getByRole('link', { name: plan.name })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText('Plan'), 'Not in a plan');
    expect(rowOf(holding.vm.name)).toBeUndefined();
  });

  it('says it does not know which plans hold the VMs while the plans cannot be loaded, never reading them free (SDD §16)', async () => {
    renderWithApp(<Inventory />, {
      route: '/inventory/rhosp17-dc1',
      path: '/inventory/:providerId',
      token: 'operator',
      wrapFetch: (mock) => (input, init) =>
        String(input).split('?')[0]!.endsWith('/api/v1/plans') ? Promise.resolve(new Response('{}', { status: 503 })) : mock(input, init),
    });
    await screen.findByRole('table', { name: /VMs on/ });
    expect(await screen.findByText('Which plans hold these VMs is unknown: the plans or their migrations could not be loaded.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Plan')).not.toBeInTheDocument();
    expect(screen.queryByText(/^In plan/)).not.toBeInTheDocument();
  });

  it('offers no Create plan on a destination inventory', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhoso-prod', path: '/inventory/:providerId', token: 'operator' });
    await screen.findByRole('heading', { level: 1, name: 'Inventory' });
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /^create plan/i })).not.toBeInTheDocument();
  });

  it('lists the destination projects that plan project mappings point to (SDD §16)', async () => {
    const { server } = renderWithApp(<Inventory />, { route: '/inventory/rhoso-prod', path: '/inventory/:providerId', token: 'viewer' });
    const projects = await screen.findByRole('region', { name: 'Projects' });
    const inventory = server.handle('GET', '/providers/rhoso-prod/inventory', new URLSearchParams(), undefined, 'viewer').body as { projects: string[] };
    expect(inventory.projects.length).toBeGreaterThan(0);
    const listed = within(projects).getAllByRole('listitem').map((item) => item.textContent);
    expect(listed).toEqual([...inventory.projects].sort((a, b) => a.localeCompare(b)));
  });

  it('shows flavor disks and Cinder quotas in GiB-based IEC units, never GB (SDD §16)', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhoso-prod', path: '/inventory/:providerId', token: 'viewer' });
    const flavors = await screen.findByRole('region', { name: 'Flavors' });
    // m1.small: a 40 GiB disk; shop: 8000 GiB of Cinder quota
    const small = within(flavors).getByRole('row', { name: /m1\.small/ });
    expect(small).toHaveTextContent('40.0 GiB');
    const quotas = screen.getByRole('region', { name: 'Free quota per project' });
    expect(within(quotas).getByRole('row', { name: /^shop/ })).toHaveTextContent('7.8 TiB');
    expect(screen.queryByText(/\bGB\b/)).not.toBeInTheDocument();
  });

  it('shows an unlimited (-1) or unreported (null) free quota as such, never as a number (SDD §10, §16)', async () => {
    const server = createTestServer();
    const inventories = (server as unknown as { inventories: Record<string, { quotas: Record<string, Record<string, number | null>> }> }).inventories;
    inventories['rhoso-prod']!.quotas = {
      admin: { cores: -1, ram_mb: -1, instances: -1, volumes: -1, gigabytes: -1 },
      ops: { cores: 10, ram_mb: null, instances: 5, volumes: 4, gigabytes: null },
    };
    renderWithApp(<Inventory />, { route: '/inventory/rhoso-prod', path: '/inventory/:providerId', token: 'viewer', server });
    const quotas = await screen.findByRole('region', { name: 'Free quota per project' });
    const cells = (project: string) =>
      within(within(quotas).getByRole('row', { name: new RegExp(`^${project}`) }))
        .getAllByRole('cell')
        .map((cell) => cell.textContent);
    expect(cells('admin')).toEqual(['Unlimited', 'Unlimited', 'Unlimited', 'Unlimited', 'Unlimited']);
    expect(cells('ops')).toEqual(['10', 'Not reported', '5', '4', 'Not reported']);
  });

  it('says when a destination reports no projects', async () => {
    renderWithApp(<Inventory />, { route: '/inventory/rhoso-staging', path: '/inventory/:providerId', token: 'viewer' });
    const projects = await screen.findByRole('region', { name: 'Projects' });
    expect(projects).toHaveTextContent('No projects reported.');
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
