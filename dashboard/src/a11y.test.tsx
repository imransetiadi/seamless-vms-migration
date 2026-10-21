import { QueryClient } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import App from './App';
import { ApiClient } from './api/client';
import { createMockFetch, type MockServer } from './api/mock';
import type { DestinationInventory } from './api/types';
import { createTestServer } from './test/utils';

/*
 * Automated WCAG checks (axe-core) on every route of the real app shell, with mock data.
 * jsdom has no layout, so colour contrast is verified separately (theme/contrast.test.ts and the
 * headless-browser audit described in the report).
 */
const ROUTES: Array<[string, string]> = [
  ['/', 'Overview'],
  ['/plans', 'Plans'],
  ['/plans/plan-4f2a9c1e', 'DC1 → RHOSO production rollout'],
  ['/migrations/mig-5d7e2b4a12', 'app-billing-01'],
  ['/migrations/mig-e5f7a9b1a0', 'legacy-rhel6-app'],
  ['/providers', 'Providers'],
  ['/users', 'Users'],
  ['/inventory/rhosp17-dc1', 'Inventory'],
  ['/inventory/rhoso-prod', 'Inventory'],
  ['/events', 'Events'],
  ['/advisor', 'Advisor'],
  ['/advisor/audit', 'Advisor audit'],
  ['/advisor/policies', 'Advisor policies'],
  ['/plans/plan-c81d44a0', 'Shared Ceph handover — analytics'],
  // a completed plan: Finalize all and the finalize queue (ADV-19)
  ['/plans/plan-2d5c8b93', 'Pilot — static content'],
  ['/advisor/recommendations/rec-d4e6f8a09001', 'Migration strategy: static-cdn-01'],
  ['/login', 'Sign in'],
];

async function violations(): Promise<Array<{ id: string; impact: string | null | undefined; help: string; targets: string[] }>> {
  const results = await axe.run(document.body, {
    rules: { 'color-contrast': { enabled: false } },
    resultTypes: ['violations'],
  });
  return results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    targets: v.nodes.slice(0, 4).map((n) => n.target.join(' ')),
  }));
}

function renderAt(path: string, token = 'admin', prepare?: (server: MockServer) => void, container?: HTMLElement) {
  window.history.pushState({}, '', path);
  const server = createTestServer();
  prepare?.(server);
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<App client={client} queryClient={queryClient} />, container ? { container } : undefined);
}

async function settled() {
  await waitFor(() => expect(screen.queryAllByText(/^(?:Loading|Checking your session)/i)).toHaveLength(0), { timeout: 8_000 });
}

beforeAll(async () => {
  await Promise.all(
    ['Overview', 'Plans', 'PlanDetail', 'MigrationDetail', 'Providers', 'Users', 'Inventory', 'Events', 'Advisor', 'AdvisorAudit', 'AdvisorPolicies', 'RecommendationDetail', 'Login'].map(
      (page) => import(`./pages/${page}.tsx`),
    ),
  );
}, 60_000);

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('axe-core: no WCAG violations', () => {
  it.each(ROUTES)('%s', async (path, heading) => {
    renderAt(path);
    expect(await screen.findByRole('heading', { level: 1, name: heading }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the full sidebar navigation (SDD §16)', async () => {
    window.localStorage.setItem('seamless.nav', 'full');
    try {
      renderAt('/');
      expect(await screen.findByRole('heading', { level: 1, name: 'Overview' }, { timeout: 8_000 })).toBeInTheDocument();
      await settled();
      expect(screen.getByTestId('app-navigation')).toHaveAttribute('data-nav', 'full');
      expect(await violations()).toEqual([]);
    } finally {
      window.localStorage.removeItem('seamless.nav');
    }
  }, 30_000);

  it('the phone navigation drawer (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    // mounted in #root as main.tsx mounts the app: an open dialog makes #root inert, the column's navigation with it
    const root = document.body.appendChild(Object.assign(document.createElement('div'), { id: 'root' }));
    renderAt('/', 'admin', undefined, root);
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    await user.click(screen.getByRole('button', { name: 'Open navigation' }));
    expect(root).toHaveAttribute('inert');
    expect(await screen.findByRole('dialog', { name: 'Navigation' })).toBeInTheDocument();
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("a migration's network editor (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    // mounted in #root as main.tsx mounts the app: the open dialog makes #root inert
    const root = document.body.appendChild(Object.assign(document.createElement('div'), { id: 'root' }));
    renderAt('/migrations/mig-b2c4d6e873', 'operator', undefined, root);
    expect(await screen.findByRole('heading', { level: 1, name: 'analytics-db' }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    const panel = screen.getByRole('region', { name: 'Network' });
    await user.click(await within(panel).findByRole('button', { name: 'Edit network' }));
    const dialog = await screen.findByRole('dialog', { name: 'Network of analytics-db' });
    expect(await violations()).toEqual([]);
    // a refused address field: marked invalid and described by its problem
    const [addresses] = within(dialog).getAllByRole('textbox');
    await user.type(addresses!, 'nonsense');
    await user.click(within(dialog).getByRole('button', { name: 'Save network' }));
    await waitFor(() => expect(addresses).toHaveAttribute('aria-invalid', 'true'));
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("the Inventory's GPU column and GPU filter, with its Technology Preview badge (SDD §16, §22.11)", async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/inventory/vcenter-hq', 'operator');
    expect(await screen.findByRole('heading', { level: 1, name: 'Inventory' }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    const table = await screen.findByRole('table', { name: /^VMs on/ });
    await user.selectOptions(screen.getByLabelText('GPU'), 'With a GPU');
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1);
    expect(await violations()).toEqual([]);
    // the badge is a focusable term: a tap shows its definition, as hover and focus do
    await user.click(within(screen.getByRole('columnheader', { name: /^GPU/ })).getByRole('button', { name: 'Technology Preview' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent(/validated in simulation/);
  }, 30_000);

  it("a destination's GPU flavors, and its GPU capacity that was not read (SDD §16, §22.11)", async () => {
    renderAt('/inventory/rhoso-prod', 'viewer', (server) => {
      const inventory = server.inventories['rhoso-prod'] as DestinationInventory;
      inventory.gpu_capacity = { status: 'unknown', reason: 'forbidden', detail: 'HTTP 403', read_at: '2026-10-08T11:59:00Z', pools: [] };
    });
    expect(await screen.findByRole('heading', { level: 1, name: 'Inventory' }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    await within(await screen.findByRole('region', { name: 'GPU flavors' })).findByRole('table');
    await within(screen.getByRole('region', { name: 'GPU capacity' })).findByText(/^Not read:/);
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("a recommendation's input tab", async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/advisor/recommendations/rec-d4e6f8a09001');
    await screen.findByRole('heading', { level: 1, name: 'Migration strategy: static-cdn-01' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('tab', { name: 'Input' }));
    await screen.findByText(/matches its SHA-256/i);
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the policy dialog with validation errors (ADV-04)', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/advisor/policies');
    await screen.findByRole('heading', { level: 1, name: 'Advisor policies' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: 'New policy' }));
    const dialog = await screen.findByRole('dialog', { name: 'New policy' });
    await user.selectOptions(within(dialog).getByLabelText('Applies to'), 'providers');
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    await within(dialog).findByRole('alert');
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the override dialog of a decision that waits (ADV-04)', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/migrations/mig-b2c4d6e871');
    await screen.findByRole('heading', { level: 1, name: 'analytics-node-2' }, { timeout: 8_000 });
    await settled();
    const group = await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' });
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    await screen.findByRole('alertdialog', { name: 'Override the recommendation for analytics-node-2?' });
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the finalize confirmation dialog', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/migrations/mig-3c1a0f9e21', 'approver');
    await screen.findByRole('heading', { level: 1, name: 'web-01' }, { timeout: 8_000 });
    await settled();
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /finalize/i }));
    await screen.findByRole('alertdialog', { name: /finalize web-01/i });
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the bulk finalize dialog of a completed plan (ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/plans/plan-2d5c8b93', 'approver');
    await screen.findByRole('heading', { level: 1, name: 'Pilot — static content' }, { timeout: 8_000 });
    await settled();
    await user.click(within(screen.getByRole('group', { name: /plan actions/i })).getByRole('button', { name: 'Finalize all' }));
    await screen.findByRole('alertdialog', { name: 'Finalize 1 VM of Pilot — static content?' });
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('a completed migration whose source VM runs again, with the demo power control (ADV-19)', async () => {
    renderAt('/migrations/mig-d4e6f8a091', 'operator', (server) => {
      server.migrations.find((m) => m.vm.name === 'web-cache-01')!.source_running_at = '2026-10-08T11:00:00Z';
    });
    await screen.findByRole('heading', { level: 1, name: 'web-cache-01' }, { timeout: 8_000 });
    await settled();
    await screen.findByRole('alert', { name: 'Source VM running' });
    await screen.findByRole('button', { name: 'Power off the source VM (demo)' });
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('a migration whose advisor decision waits for approval, its approvals named by kind (ADV-20)', async () => {
    const held = createTestServer().migrations.find((m) => m.vm.name === 'analytics-node-2')!;
    renderAt(`/migrations/${held.id}`, 'viewer');
    await screen.findByRole('heading', { level: 1, name: 'analytics-node-2' }, { timeout: 8_000 });
    await settled();
    await within(screen.getByRole('region', { name: 'Approvals' })).findByText('Advisor decision · Migration strategy');
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the new-plan dialog with validation errors', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/plans', 'operator');
    await screen.findByRole('heading', { level: 1, name: 'Plans' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));
    await within(dialog).findByRole('alert');
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the provider dialog: platform tiles, sign-in methods, conversion host and errors', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/providers', 'admin');
    await screen.findByRole('heading', { level: 1, name: 'Providers' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: /^add provider$/i }));
    const dialog = await screen.findByRole('dialog', { name: /^add provider$/i });
    await user.click(within(dialog).getByRole('radio', { name: /application credential/i }));
    await user.click(within(dialog).getByRole('button', { name: /configure/i }));
    await user.click(within(dialog).getByRole('checkbox', { name: /let seamless deploy/i }));
    await user.click(within(dialog).getByRole('button', { name: /add and test connection/i }));
    await within(dialog).findByRole('alert');
    expect(await violations()).toEqual([]);
    await user.click(within(dialog).getByRole('radio', { name: /vmware vcenter/i }));
    expect(await violations()).toEqual([]);
  }, 30_000);
});
