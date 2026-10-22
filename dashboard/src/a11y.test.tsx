import { QueryClient } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import App from './App';
import { ApiClient } from './api/client';
import { createMockFetch, type MockServer } from './api/mock';
import { makeVm } from './api/mockData';
import type { DestinationInventory, VMRef } from './api/types';
import { gpuDevice } from './lib/gpu';
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
  ['/hooks', 'Hooks'],
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
    ['Overview', 'Plans', 'PlanDetail', 'MigrationDetail', 'Providers', 'Users', 'Hooks', 'Inventory', 'Events', 'Advisor', 'AdvisorAudit', 'AdvisorPolicies', 'RecommendationDetail', 'Login'].map(
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

  it("a destination card's GPU capacity that was not read, and the provider form's vGPU profiles with their errors (SDD §16, §22.11)", async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/providers', 'admin', (server) => {
      const hibiscus = server.providers.find((p) => p.id === 'hibiscus-dc3')!;
      hibiscus.capabilities = { ...hibiscus.capabilities, gpu_capacity: { status: 'unknown', reason: 'forbidden', detail: 'HTTP 403', read_at: hibiscus.last_checked_at, pools: [] } };
    });
    await screen.findByRole('heading', { level: 1, name: 'Providers' }, { timeout: 8_000 });
    await settled();
    const card = screen.getByRole('article', { name: 'OpenStack 2026.2 Hibiscus (DC3)' });
    await within(within(card).getByRole('group', { name: 'GPU capacity' })).findByText(/^Not read:/);
    expect(await violations()).toEqual([]);
    await user.click(within(card).getByRole('button', { name: /^edit /i }));
    const dialog = await screen.findByRole('dialog');
    const section = within(dialog).getByRole('region', { name: 'vGPU profiles' });
    await within(section).findByText(/^Its GPU flavors/);
    await user.click(within(section).getByRole('button', { name: 'Add vGPU profile' }));
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));
    await within(dialog).findByRole('alert');
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("the plan form's GPU mappings, with a flavor that does not fit and a device no mapping can take (SDD §16, §22.11)", async () => {
    const user = userEvent.setup({ delay: null });
    // mounted in #root as main.tsx mounts the app: the open dialog makes #root inert
    const root = document.body.appendChild(Object.assign(document.createElement('div'), { id: 'root' }));
    renderAt(
      '/plans',
      'operator',
      (server) => {
        const inventory = server.inventories['vcenter-hq'] as VMRef[];
        inventory.push(makeVm({ id: 'vm-5011', name: 'dc2-fpga-01', disks: [{ size_gb: 40, kind: 'vmdk', volume_type: null }], gpus: [gpuDevice({ kind: 'passthrough' })] }));
      },
      root,
    );
    await screen.findByRole('heading', { level: 1, name: 'Plans' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: 'New plan' }));
    const dialog = await screen.findByRole('dialog', { name: 'New migration plan' });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'vcenter-hq');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select dc2-ml-02' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select dc2-fpga-01' }));
    const row = within(dialog).getByRole('combobox', { name: 'grid_a100-3-20c: 1 VM, 16 vCPUs, 64 GiB' });
    await within(row).findByRole('option', { name: /^g1\.a100-4c:/ });
    await user.selectOptions(row, 'g1.a100-4c');
    expect(row).toHaveAccessibleDescription(/Does not fit dc2-ml-02/);
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("a migration's GPU panel while its cutover waits for GPU capacity (SDD §16, §22.11)", async () => {
    renderAt('/migrations/mig-9e4f6a8b51', 'viewer', (server) => {
      // a VMware migration of the MIG-backed vGPU VM, its key mapped, waiting at its gate (SDD §22.7)
      const m = server.migrations.find((x) => x.id === 'mig-9e4f6a8b51')!;
      m.vm = (server.inventories['vcenter-hq'] as VMRef[]).find((v) => v.source_id === 'vm-5008')!;
      const plan = server.plans.find((p) => p.id === m.plan_id)!;
      plan.mappings = { ...plan.mappings, gpus: { 'grid_a100-3-20c': 'g1.a100-3-20c' } };
      m.gpu_wait = {
        since: '2026-10-08T11:58:00Z',
        checked_at: '2026-10-08T11:59:30Z',
        pool: 'VGPU CUSTOM_NVIDIA_A100_3_20C',
        needed: 1,
        free: 0,
        reason: "VGPU CUSTOM_NVIDIA_A100_3_20C of RHOSO 18.0 — Production has 0 units free, and flavor 'g1.a100-3-20c' needs 1",
      };
    });
    expect(await screen.findByRole('heading', { level: 1, name: 'dc2-ml-02' }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    await within(screen.getByRole('region', { name: 'GPU' })).findByText(/^Waiting for GPU capacity: 0 free of 1/);
    expect(await violations()).toEqual([]);
  }, 30_000);

  it.each([
    ['/', 'Overview'],
    ['/plans/plan-4f2a9c1e', 'DC1 → RHOSO production rollout'],
  ])("%s with a migration waiting for GPU capacity, its Technology Preview badge (SDD §16, §22.11)", async (path, heading) => {
    renderAt(path, 'viewer', (server) => {
      const billing = server.migrations.find((m) => m.vm.name === 'app-billing-01')!;
      server.plans.find((p) => p.id === billing.plan_id)!.cutover_window = null;
      Object.assign(billing, {
        approvals: [{ actor: 'sari', at: new Date().toISOString(), comment: null }],
        cutover_requested: true,
        gpu_wait: { since: new Date().toISOString(), checked_at: new Date().toISOString(), pool: 'VGPU CUSTOM_NVIDIA_A100_4C', needed: 1, free: 0, reason: 'VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 — Production has 0 units free' },
      });
    });
    expect(await screen.findByRole('heading', { level: 1, name: heading }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    await screen.findByText(/^Waiting for GPU capacity/);
    expect(screen.getAllByRole('button', { name: 'Technology Preview' }).length).toBeGreaterThan(0);
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

  it('the hook catalog: a hook, its register form refused, and the delete confirmation (SDD §16, plan task N12a)', async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/hooks', 'admin', (server) => {
      const at = '2026-10-08T12:00:00Z';
      server.hooks.set('drain-lb', { name: 'drain-lb', kind: 'playbook', description: 'Drain the member', playbook: '/opt/hooks/drain-lb.yml', url: null, secret: null, timeout_s: 120, revert: null, created_at: at, updated_at: at });
    });
    await screen.findByRole('heading', { level: 1, name: 'Hooks' }, { timeout: 8_000 });
    await settled();
    await screen.findByRole('listitem', { name: 'drain-lb' });
    await user.selectOptions(screen.getByLabelText('Kind'), 'webhook');
    await user.type(screen.getByLabelText(/^Name/), 'notify');
    await user.type(screen.getByLabelText(/^URL/), 'http://hooks.example/notify');
    await user.type(screen.getByLabelText(/^Secret/), 'notify-token');
    await user.click(screen.getByRole('button', { name: 'Register hook' }));
    await screen.findByRole('alert');
    expect(await violations()).toEqual([]);
    await user.click(screen.getByRole('button', { name: 'Delete drain-lb' }));
    await screen.findByRole('alertdialog');
    expect(await violations()).toEqual([]);
  }, 30_000);

  it("the plan form's network cut-over: hooks, a DNS target and a pool mapping, then its error (SDD §16, plan task N12b)", async () => {
    const user = userEvent.setup({ delay: null });
    renderAt('/plans', 'operator', (server) => {
      const at = '2026-10-08T12:00:00Z';
      for (const name of ['drain-lb', 'smoke'])
        server.hooks.set(name, { name, kind: 'playbook', description: null, playbook: `/opt/hooks/${name}.yml`, url: null, secret: null, timeout_s: 300, revert: null, created_at: at, updated_at: at });
    });
    await screen.findByRole('heading', { level: 1, name: 'Plans' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const section = within(dialog).getByRole('group', { name: /^network cut-over/i });
    const preStop = within(section).getByRole('group', { name: /^before the stop/i });
    await user.selectOptions(await within(preStop).findByLabelText(/^hook to add/i), 'drain-lb');
    await user.click(within(preStop).getByRole('button', { name: /^add hook/i }));
    await user.click(within(section).getByRole('button', { name: 'Add DNS target' }));
    await user.selectOptions(within(section).getByLabelText(/^DNS target 1 kind/), 'rfc2136');
    await user.click(within(section).getByRole('button', { name: 'Add pool mapping' }));
    expect(await violations()).toEqual([]);
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));
    await within(dialog).findByRole('alert');
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
