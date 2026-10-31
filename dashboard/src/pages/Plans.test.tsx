import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { MockServer } from '../api/mock';
import { makeVm } from '../api/mockData';
import type { VMRef } from '../api/types';
import { Layout } from '../components/Layout';
import { gpuDevice } from '../lib/gpu';
import { phaseMeta } from '../lib/phase';
import { createTestServer, formFields, recordScrolls, renderWithApp } from '../test/utils';
import Inventory from './Inventory';
import Plans from './Plans';


/** A source VM whose one volume has volume type `type` on `pool`, listed first in the source's inventory. */
function withVolumeType(server: MockServer, id: string, type: string, pool: string): void {
  const inventory = (server as unknown as { inventories: Record<string, VMRef[]> }).inventories['rhosp17-dc1']!;
  inventory.unshift(makeVm({ id, name: 'odd-01', project: 'shop', disks: [{ size_gb: 20, used_gb: 2, volume_type: type, pool }] }));
}

/** A VMware VM of the source's inventory, on its ERP port group, with these GPUs (SDD §22.2). */
function withVmwareGpuVm(server: MockServer, id: string, name: string, gpus: VMRef['gpus'], size: { vcpus?: number; ram_mb?: number } = {}): void {
  const inventory = (server as unknown as { inventories: Record<string, VMRef[]> }).inventories['vcenter-hq']!;
  inventory.push(
    makeVm({ id, name, project: 'erp', ...size, cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 100, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], gpus }),
  );
}

/** The new-plan dialog with its name, source and destination chosen. */
async function newPlan(user: ReturnType<typeof userEvent.setup>, name: string, source: string) {
  await user.click(await screen.findByRole('button', { name: /new plan/i }));
  const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
  await user.type(within(dialog).getByLabelText(/^name/i), name);
  await user.selectOptions(within(dialog).getByLabelText(/source provider/i), source);
  await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
  return dialog;
}

describe('Plans page', () => {
  it('maps each GPU key of the selected VMware VMs to a destination GPU flavor, never prefilled', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'ML exit', 'vcenter-hq');
    // no selected VM has a GPU: no GPU mappings
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select vm-erp-app-01' }));
    expect(within(dialog).queryByRole('group', { name: 'GPU mappings' })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select dc2-ml-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select dc2-ml-02' }));
    const section = within(dialog).getByRole('group', { name: 'GPU mappings' });
    // outside the advanced settings, with its Technology Preview badge
    expect(within(dialog).getByText(/^advanced:/i).closest('details')).not.toContainElement(section);
    expect(within(section).getByRole('button', { name: 'Technology Preview' })).toBeInTheDocument();
    // one row per GPU key, by key, naming how many VMs have it and their size; nothing chosen for the operator
    const mig = within(section).getByRole('combobox', { name: 'grid_a100-3-20c: 1 VM, 16 vCPUs, 64 GiB' });
    const fourC = within(section).getByRole('combobox', { name: 'grid_a100-4c: 1 VM, 8 vCPUs, 32 GiB' });
    expect(within(section).getAllByRole('combobox')).toEqual([mig, fourC]);
    expect([mig, fourC].map((select) => (select as HTMLSelectElement).value)).toEqual(['', '']);
    // a choice among the destination's GPU flavors, each with its size and GPU requests, or None
    await waitFor(() =>
      expect(within(fourC).getAllByRole('option').map((o) => o.textContent)).toEqual([
        'None',
        'g1.a100-4c: 8 vCPUs, 32 GiB RAM, 200 GiB disk, vGPU A100-4C · VGPU, CUSTOM_NVIDIA_A100_4C',
        'g1.a100-3-20c: 16 vCPUs, 64 GiB RAM, 300 GiB disk, vGPU A100-3-20C · VGPU, CUSTOM_NVIDIA_A100_3_20C',
        'p1.a100: 16 vCPUs, 128 GiB RAM, 400 GiB disk, Passthrough · alias a100',
      ]),
    );
    await user.selectOptions(fourC, 'g1.a100-4c');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 3 vms/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'ML exit')).toBe(true));
    // the key left at None is no mapping: validation keeps its VM blocked
    expect(server.plans.find((p) => p.name === 'ML exit')!.mappings.gpus).toEqual({ 'grid_a100-4c': 'g1.a100-4c' });
  });

  it.each(['__proto__', 'constructor'])('maps a GPU key named %s as any other, by own key (SDD §22.4, G12 review)', async (key) => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    withVmwareGpuVm(server, 'vm-5012', 'odd-gpu-01', [gpuDevice({ kind: 'vgpu', profile: key })]);
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    const dialog = await newPlan(user, 'Odd key', 'vcenter-hq');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select odd-gpu-01' }));
    const row = within(dialog).getByRole('combobox', { name: `${key}: 1 VM, 2 vCPUs, 4 GiB` });
    await within(row).findByRole('option', { name: /^g1\.a100-4c:/ });
    await user.selectOptions(row, 'g1.a100-4c');
    expect(row).toHaveValue('g1.a100-4c');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Odd key')).toBe(true));
    expect(Object.entries(server.plans.find((p) => p.name === 'Odd key')!.mappings.gpus ?? {})).toEqual([[key, 'g1.a100-4c']]);
  });

  it('marks the VMs a GPU flavor does not fit', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    withVmwareGpuVm(server, 'vm-5010', 'dc2-ml-03', [gpuDevice({ kind: 'vgpu', profile: 'grid_a100-4c' })], { vcpus: 16, ram_mb: 65536 });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    const dialog = await newPlan(user, 'ML sizes', 'vcenter-hq');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select dc2-ml-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select dc2-ml-03' }));
    const row = within(dialog).getByRole('combobox', { name: 'grid_a100-4c: 2 VMs, 8–16 vCPUs, 32–64 GiB' });
    await within(row).findByRole('option', { name: /^g1\.a100-4c:/ });
    // the GPU flavor decides the server's size, as MAP_GPU_FLAVOR warns: each VM it does not fit, with what it gives
    await user.selectOptions(row, 'g1.a100-4c');
    expect(row).toHaveAccessibleDescription(/Does not fit dc2-ml-03: 8 vCPUs for its 16 and 32 GiB RAM for its 64 GiB\./);
    expect(row).not.toHaveAccessibleDescription(/dc2-ml-01:/);
    // a flavor of both VMs' size marks none
    await user.selectOptions(row, 'g1.a100-3-20c');
    expect(row).not.toHaveAccessibleDescription(/Does not fit/);
  });

  it('names a passthrough device no mapping can take', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    withVmwareGpuVm(server, 'vm-5011', 'dc2-fpga-01', [gpuDevice({ kind: 'passthrough' })]);
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    const dialog = await newPlan(user, 'FPGA', 'vcenter-hq');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select dc2-fpga-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select dc2-cuda-01' }));
    const section = within(dialog).getByRole('group', { name: 'GPU mappings' });
    // a device without ids has no GPU key: named with its VM, and no choice offered for it (SDD §22.2, §22.4)
    expect(section).toHaveTextContent('dc2-fpga-01: Passthrough without device ids. No mapping can take a device without ids: validation blocks the VM.');
    // the device with ids is mapped by them
    expect(within(section).getAllByRole('combobox')).toHaveLength(1);
    expect(within(section).getByRole('combobox', { name: '10de:20b5: 1 VM, 16 vCPUs, 128 GiB' })).toHaveValue('');
  });

  it("says which selected VMs' flavors ask for a GPU and offers the destination's GPU flavors for them (SDD §16, §22.4)", async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'ML training', 'rhosp17-dc1');
    // past the table's first page of 25: found by its name
    await user.type(await within(dialog).findByLabelText('Search VMs'), 'gpu-ml');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select gpu-ml-01' }));
    const section = within(dialog).getByRole('group', { name: 'GPU mappings' });
    // an OpenStack VM's GPU comes with its flavor: its flavor mapping is its GPU mapping, offered here and in the field
    const row = within(section).getByRole('listitem', { name: 'g1.vgpu: 1 VM, 8 vCPUs, 32 GiB' });
    expect(row).toHaveTextContent('Asks for vGPU · VGPU, CUSTOM_NVIDIA_A100_4C');
    expect(row).toHaveTextContent('Not mapped: validation blocks its VMs until Flavor mappings map it to a GPU flavor.');
    await user.click(within(section).getByRole('link', { name: 'Edit flavor mappings' }));
    const field = within(dialog).getByLabelText(/^flavor mappings/i);
    expect(field).toHaveFocus();
    await waitFor(() =>
      expect(field).toHaveAccessibleDescription(
        'g1.vgpu (gpu-ml-01) asks for a GPU: map it to a GPU flavor of RHOSO 18.0 — Production - g1.a100-4c, g1.a100-3-20c or p1.a100. A same-named flavor is no mapping.',
      ),
    );
    await user.type(field, 'g1.vgpu = m1.large');
    expect(row).toHaveTextContent('Flavor mappings map it to m1.large, which asks for no GPU: its server would lose its GPU, so validation blocks it.');
    await user.clear(field);
    await user.type(field, 'g1.vgpu = g1.a100-4c');
    expect(row).toHaveTextContent('Flavor mappings map it to g1.a100-4c: 8 vCPUs, 32 GiB RAM, 200 GiB disk, vGPU A100-4C · VGPU, CUSTOM_NVIDIA_A100_4C.');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'ML training')).toBe(true));
    expect(server.plans.find((p) => p.name === 'ML training')!.mappings).toMatchObject({ flavors: { 'g1.vgpu': 'g1.a100-4c' }, gpus: {} });
  });

  it('lists plans with status in text', async () => {
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer' });
    const table = await screen.findByRole('table', { name: /migration plans/i });
    expect(within(table).getAllByRole('row')).toHaveLength(server.plans.length + 1);
    expect(within(table).getAllByText('Paused')).toHaveLength(2);
    expect(screen.getByRole('button', { name: /new plan/i })).toHaveAttribute('aria-disabled', 'true');
  });

  it("keeps each plan's done count and clouds under its name for a phone, where their columns are hidden (SDD §16)", async () => {
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer' });
    const link = await screen.findByRole('link', { name: 'DC1 → RHOSO production rollout' });
    const header = link.closest('th')!;
    await waitFor(() => expect(within(header).getByText(/^\d+\/10 done$/)).toBeInTheDocument());
    expect(within(header).getByText(/RHOSP 17\.1 — DC1 → RHOSO 18\.0 — Production/)).toBeInTheDocument();
    // shown only below the sm breakpoint, where the Done and clouds columns are hidden; no minimum width there
    expect(within(header).getByText(/done$/).closest('.sm\\:hidden')).not.toBeNull();
    expect(header.className).not.toMatch(/(^|\s)min-w-48/);
  });

  it('shows each plan with the status its migrations tell, and filters by it (SDD §8.1, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    plan.status = 'validated'; // a re-validated finished plan
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const row = (await screen.findByRole('link', { name: plan.name })).closest('tr')!;
    expect(row).toHaveTextContent('Completed');
    expect(row).not.toHaveTextContent('Validated');
    await user.selectOptions(screen.getByLabelText(/^status/i), 'completed');
    expect(screen.getByRole('link', { name: plan.name })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText(/^status/i), 'validated');
    expect(screen.queryByRole('link', { name: plan.name })).not.toBeInTheDocument();
  });

  it('marks a Completed plan where nothing was migrated, and only that one (SDD §16)', async () => {
    const server = createTestServer();
    for (const m of server.migrations.filter((x) => x.plan_id === 'plan-2d5c8b93')) m.phase = 'rolled_back';
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const row = (await screen.findByRole('link', { name: 'Pilot — static content' })).closest('tr')!;
    for (const badge of within(row).getAllByRole('button', { name: 'Completed' })) {
      expect(badge).toHaveAccessibleDescription('Nothing was migrated: every migration rolled back.');
    }
    // Mobile and desktop copies belong only to the plan that migrated nothing.
    expect(screen.queryAllByRole('button', { name: 'Completed' })).toHaveLength(2);
  });

  it('says the providers are unavailable when the list shows provider ids instead of names (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/providers'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/providers are unavailable/i);
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it("puts the focus on the page's heading when a successful Retry takes the failure away (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const handle = server.handle.bind(server);
    let failing = true;
    server.handle = (method, path, query, body, token) =>
      failing && method === 'GET' && path === '/plans'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused once for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const alert = await screen.findByRole('alert');
    failing = false;
    await user.click(within(alert).getByRole('button', { name: /retry/i }));
    await screen.findByRole('table', { name: /migration plans/i });
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Plans' })).toHaveFocus());
  });

  it('reads no migration on a live event while the plan form is closed (SDD §16, audit AUD-1)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    const read: string[] = [];
    server.handle = (method, path, query, body, token) => {
      if (method === 'GET') read.push(path);
      return handle(method, path, query, body, token);
    };
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server, live: true });
    await screen.findByRole('table', { name: /migration plans/i });
    const m = server.migrations[0]!;
    const phase = (seq: number) =>
      server.emit({ kind: 'migration.phase', plan_id: m.plan_id, migration_id: m.id, actor: 'orchestrator', message: `phase ${seq}`, data: {} });
    // the closed form's summary query is disabled: no page shows a migration, so no event reads one
    for (const seq of [1, 2, 3]) phase(seq);
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(read.filter((path) => path.startsWith('/migrations'))).toEqual([]);
    // the open form shows the summaries: the next event reads that migration, once
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    await screen.findByRole('dialog', { name: /new migration plan/i });
    await waitFor(() => expect(read).toContain('/migrations'));
    phase(4);
    await waitFor(() => expect(read).toContain(`/migrations/${m.id}`), { timeout: 3000 });
  });

  it('counts a plan done over its migrations that are not cancelled, and a cancelled plan shows no count (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.name === 'DC1 → RHOSO production rollout')!;
    const ofPlan = server.migrations.filter((m) => m.plan_id === plan.id);
    for (const m of ofPlan) m.phase = 'finalized';
    // a VM taken out of the plan left a cancelled migration: never done, so out of the count
    ofPlan[0]!.phase = 'cancelled';
    renderWithApp(<Plans />, { server });
    const link = await screen.findByRole('link', { name: plan.name });
    const n = ofPlan.length - 1;
    // and named: the plan's VMs and its done total never differ without a word
    expect(link.closest('th')!).toHaveTextContent(`${n}/${n} done, 1 cancelled`);
    const cancelled = server.plans.find((p) => p.status === 'cancelled')!;
    const row = screen.getByRole('link', { name: cancelled.name }).closest('tr')!;
    expect(within(row).queryByText(/\d+\/\d+/)).not.toBeInTheDocument();
    expect(within(row).getAllByText('Nothing left to do').length).toBeGreaterThan(0);
  });

  it("shows each plan's done count from its migration counts and reads no migration (SDD §16, PF-02)", async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    const read: string[] = [];
    server.handle = (method, path, query, body, token) => {
      if (method === 'GET') read.push(path);
      return handle(method, path, query, body, token);
    };
    const plan = server.plans.find((p) => p.name === 'DC1 → RHOSO production rollout')!;
    const ofPlan = server.migrations.filter((m) => m.plan_id === plan.id);
    // completed and finalized both count as done
    ofPlan[0]!.phase = 'completed';
    ofPlan[1]!.phase = 'finalized';
    const done = ofPlan.filter((m) => m.phase === 'completed' || m.phase === 'finalized').length;
    // a plan before its first validation counts its VMs, none done
    server.plans.push({ ...plan, id: 'plan-0d0d0d0d', name: 'Not validated yet', vm_ids: ['vm-a', 'vm-b', 'vm-c'], waves: [], status: 'draft' });
    renderWithApp(<Plans />, { server });
    const link = await screen.findByRole('link', { name: 'DC1 → RHOSO production rollout' });
    expect(within(link.closest('th')!).getByText(`${done}/${ofPlan.length} done`)).toBeInTheDocument();
    expect(within(screen.getByRole('link', { name: 'Not validated yet' }).closest('th')!).getByText('0/3 done')).toBeInTheDocument();
    expect(read).toContain('/plans');
    expect(read.filter((path) => path.startsWith('/migrations'))).toEqual([]);
  });

  it("names the VMs another plan holds from the migration summaries it reads while open (SDD §5.4, §16, perf audit PF-02)", async () => {
    const user = userEvent.setup({ delay: null });
    const reads: string[] = [];
    renderWithApp(<Plans />, {
      route: '/plans',
      path: '/plans',
      token: 'operator',
      wrapFetch: (mock) => (input, init) => {
        if ((init?.method ?? 'GET').toUpperCase() === 'GET') reads.push(String(input).replace(/^.*\/api\/v1/, ''));
        return mock(input, init);
      },
    });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    await screen.findByRole('dialog', { name: /new migration plan/i });
    await waitFor(() => expect(reads.some((r) => r.startsWith('/migrations'))).toBe(true));
    expect(reads.filter((r) => r.startsWith('/migrations'))).toEqual(['/migrations?view=summary']);
  });

  it('summarises validation errors and moves focus to the summary', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));

    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/fix 4 problems/i);
    await waitFor(() => expect(summary).toHaveFocus());
    expect(within(dialog).getByLabelText(/^name/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('resets VM filters when changing the source to an already cached inventory', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = within(screen.getByRole('dialog', { name: /new migration plan/i }));
    const source = dialog.getByLabelText(/source provider/i);
    await user.selectOptions(source, 'vcenter-hq');
    await dialog.findByLabelText('Search VMs');
    await user.selectOptions(source, 'rhosp17-dc1');
    await dialog.findByRole('checkbox', { name: 'Select web-01' });
    await user.type(dialog.getByLabelText('Search VMs'), 'web-01');
    await user.selectOptions(source, 'vcenter-hq');
    expect(dialog.getByLabelText('Search VMs')).toHaveValue('');
    expect(dialog.getByRole('table', { name: /^VMs on vCenter HQ/ })).toBeInTheDocument();
    expect(source).toHaveFocus();
  });

  it('creates a draft plan with the selected VMs', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Wave test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select web-02' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 2 vms/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Wave test')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Wave test');
    expect(created?.vm_ids).toEqual(['os-0a11', 'os-0a12']);
    expect(created?.downtime_slo_s).toBe(600);
    expect(created?.estimator_overrides).toEqual({});
    expect(created?.status).toBe('draft');
  });

  it('scrolls a plan that was not created into view (SDD §16)', async () => {
    const recorder = recordScrolls();
    try {
      const user = userEvent.setup({ delay: null });
      const server = createTestServer();
      const handle = server.handle.bind(server);
      server.handle = (method, path, query, body, token) =>
        method === 'POST' && path === '/plans'
          ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
          : handle(method, path, query, body, token);
      renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
      await user.click(await screen.findByRole('button', { name: /new plan/i }));
      const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
      await user.type(within(dialog).getByLabelText(/^name/i), 'Not created');
      await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
      await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
      await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
      await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
      const alert = await within(dialog).findByText('The plan was not created');
      const banner = alert.closest('[role="alert"]');
      expect(banner).not.toBeNull();
      expect(recorder.scrolls.map((s) => s.element)).toContain(banner);
    } finally {
      recorder.restore();
    }
  });

  it('sets the guest write rate and the aggregate scan cap of a new plan (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Write rate test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await user.type(within(dialog).getByLabelText(/guest write rate/i), '8');
    await user.type(within(dialog).getByLabelText(/aggregate scan cap/i), '1000');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Write rate test')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Write rate test')?.estimator_overrides).toEqual({
      change_rate_bps: 8 * 2 ** 20,
      max_aggregate_scan_bps: 1000 * 2 ** 20,
    });
  });

  it('sets the measured step times of a new plan: shutdown, snapshot, create and boot (Performance.md §6.4, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Step times');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    // empty fields keep the planning defaults, which the placeholders show
    expect(field(/source shutdown/i)).toHaveAttribute('placeholder', '60 (planning default)');
    expect(field(/^boot/i)).toHaveAttribute('placeholder', '120 (planning default)');
    await enter(/source shutdown/i, '30');
    await enter(/snapshot/i, '20');
    await enter(/volume create/i, '45');
    await enter(/^boot/i, '90');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Step times')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Step times')?.estimator_overrides).toEqual({ shutdown_s: 30, snapshot_s: 20, create_s: 45, boot_s: 90 });
  }, 30_000);

  it('refuses a step time that is not more than 0, like the API (SDD §9.1)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const before = server.plans.length;
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Bad step times');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await enter(/source shutdown/i, '0');
    await enter(/^boot/i, '-5');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Enter the source shutdown time in seconds (more than 0, at most 1000000000), or leave it empty.');
    expect(summary).toHaveTextContent('Enter the boot time in seconds (more than 0, at most 1000000000), or leave it empty.');
    expect(server.plans).toHaveLength(before);
  }, 30_000);

  it('refuses a guest write rate or aggregate scan cap that is not more than 0, like the API (SDD §9.1)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const before = server.plans.length;
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Bad rates');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await enter(/guest write rate/i, '0');
    await enter(/aggregate scan cap/i, '-5');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Enter the guest write rate in MiB/s (more than 0), or leave it empty.');
    expect(summary).toHaveTextContent('Enter the aggregate scan cap in MiB/s (at least 1 B/s), or leave it empty.');
    expect(server.plans).toHaveLength(before);
  });

  it('accepts the sync values the API accepts: threshold 0, 60 passes, 100 disks in parallel (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'API bounds');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    for (const [label, value] of [
      [/convergence threshold/i, '0'],
      [/max sync passes/i, '60'],
      [/disks scanned in parallel/i, '100'],
    ] as const)
      await enter(label, value);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'API bounds')).toBe(true));
    const created = server.plans.find((p) => p.name === 'API bounds')!;
    expect([created.convergence_threshold_bytes, created.max_sync_passes, created.estimator_overrides.parallel_disks]).toEqual([0, 60, 100]);
  }, 30_000);

  it('refuses a negative threshold, 0 passes and 0 disks in parallel, like the API (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const before = server.plans.length;
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Below the bounds');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    for (const [label, value] of [
      [/convergence threshold/i, '-1'],
      [/max sync passes/i, '0'],
      [/disks scanned in parallel/i, '0'],
    ] as const)
      await enter(label, value);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Enter the convergence threshold in GiB (0 or more).');
    expect(summary).toHaveTextContent('Enter a whole number of passes from 1 to 100.');
    expect(summary).toHaveTextContent('Enter a whole number of disks (1 or more), or leave it empty.');
    // an empty threshold is not 0
    await user.clear(field(/convergence threshold/i));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(field(/convergence threshold/i)).toHaveAccessibleDescription(/0 or more/));
    expect(server.plans).toHaveLength(before);
  }, 30_000);

  it('refuses 101 passes, a link or scan rate under 1 B/s and a step time over 10^9 s, like the API (SDD §9.1, reviews RV51-07, RV51-22)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const before = server.plans.length;
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Past the bounds');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    for (const [label, value] of [
      [/max sync passes/i, '101'],
      [/link bandwidth/i, '0.0000001'],
      [/^scan rate/i, '0.0000001'],
      [/aggregate scan cap/i, '0.0000001'],
      [/^boot/i, '1000000001'],
    ] as const)
      await enter(label, value);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Enter a whole number of passes from 1 to 100.');
    expect(summary).toHaveTextContent('Enter the link bandwidth in MiB/s (at least 1 B/s).');
    expect(summary).toHaveTextContent('Enter the scan rate in MiB/s (at least 1 B/s), or leave it empty.');
    expect(summary).toHaveTextContent('Enter the aggregate scan cap in MiB/s (at least 1 B/s), or leave it empty.');
    expect(summary).toHaveTextContent('Enter the boot time in seconds (more than 0, at most 1000000000), or leave it empty.');
    expect(server.plans).toHaveLength(before);
  }, 30_000);

  it('gives an operator the default approval policy, read-only (SDD §12, §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const approval = within(dialog).getByRole('checkbox', { name: /require approval/i });
    expect(approval).toBeChecked();
    expect(approval).toBeDisabled();
    const auto = within(dialog).getByRole('checkbox', { name: /automatic cutover/i });
    expect(auto).not.toBeChecked();
    expect(auto).toBeDisabled();
    expect(within(dialog).getByLabelText(/cutover window start/i)).toBeDisabled();
  });

  it('says how many plans its filters show from a live region already on the page, also when none match (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer' });
    const n = server.plans.length;
    const status = await screen.findByText(`Showing ${n} of ${n} plans`);
    expect(status).toHaveAttribute('role', 'status');
    await user.type(screen.getByRole('searchbox', { name: /search plans/i }), 'zzzz-no-match');
    expect(status).toHaveTextContent(`Showing 0 of ${n} plans`);
    expect(screen.getByText('No plans match these filters')).toBeInTheDocument();
  });

  it("names the cancelled migrations a plan's done figure leaves out, so its VMs and its total never differ without a word (SDD §16)", async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-95a7e3f1')!;
    const mine = server.migrations.filter((m) => m.plan_id === plan.id);
    const cancelled = mine.filter((m) => m.phase === 'cancelled').length;
    expect(cancelled).toBe(1);
    const done = mine.filter((m) => m.phase === 'completed' || m.phase === 'finalized').length;
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const row = (await screen.findByRole('rowheader', { name: new RegExp(plan.name) })).closest('tr')!;
    expect(row).toHaveTextContent(`${done}/${mine.length - cancelled}, 1 cancelled`);
  });

  it('names them on a line of their own under the bar, never beside it, where they squeezed the bar to a dot (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.id === 'plan-95a7e3f1')!;
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer', server });
    const row = (await screen.findByRole('rowheader', { name: new RegExp(plan.name) })).closest('tr')!;
    // the line under the name (a phone) and the Done column each keep the bar beside its count alone
    const bars = within(row).getAllByRole('progressbar');
    expect(bars).toHaveLength(2);
    for (const bar of bars) {
      expect(bar.parentElement).not.toHaveTextContent(/cancelled/);
      expect(bar.parentElement!.nextElementSibling).toHaveTextContent(/^, 1 cancelled$/);
    }
    // and only when it leaves some out
    const whole = server.plans.find(
      (p) => p.status !== 'cancelled' && server.migrations.some((m) => m.plan_id === p.id) && !server.migrations.some((m) => m.plan_id === p.id && m.phase === 'cancelled'),
    )!;
    expect(screen.getByRole('link', { name: whole.name }).closest('tr')).not.toHaveTextContent(/cancelled/i);
  });

  it('names a selected VM that another plan holds, as validation would refuse it (SDD §5.4, §16)', async () => {
    const server = createTestServer();
    const holder = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === holder.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), holder.source_provider_id);
    await user.click(await within(dialog).findByRole('checkbox', { name: `Select ${held.vm.name}` }));

    const note = await within(dialog).findByRole('status', { name: /another plan holds/i });
    expect(note).toHaveTextContent(`${held.vm.name} (plan "${holder.name}", ${holder.id}, ${held.phase})`);
  });

  it('names in the VM picker the plan that holds each VM before any is selected, with the Plan filter (SDD §16)', async () => {
    const server = createTestServer();
    const holder = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === holder.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), holder.source_provider_id);
    const row = (await within(dialog).findByRole('checkbox', { name: `Select ${held.vm.name}` })).closest('tr')!;
    await waitFor(() => expect(within(row).getByRole('link', { name: holder.name })).toHaveAttribute('href', `/plans/${holder.id}`));
    expect(row).toHaveTextContent(`In plan ${holder.name}, phase ${phaseMeta(held.phase).label}`);
    await user.selectOptions(within(dialog).getByLabelText('Plan'), 'Not in a plan');
    expect(within(dialog).queryByRole('checkbox', { name: `Select ${held.vm.name}` })).not.toBeInTheDocument();
  });

  it('says in the VM picker that it does not know which plans hold the VMs while the migrations cannot be loaded (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, {
      route: '/plans',
      path: '/plans',
      token: 'operator',
      wrapFetch: (mock) => (input, init) =>
        String(input).split('?')[0]!.endsWith('/api/v1/migrations') ? Promise.resolve(new Response('{}', { status: 503 })) : mock(input, init),
    });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    expect(await within(dialog).findByText('Which plans hold these VMs is unknown: the plans or their migrations could not be loaded.')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Plan')).not.toBeInTheDocument();
  });

  it('announces a VM another plan holds from a live region already in the form (SDD §16)', async () => {
    const server = createTestServer();
    const holder = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === holder.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), holder.source_provider_id);
    const checkbox = await within(dialog).findByRole('checkbox', { name: `Select ${held.vm.name}` });
    const regions = within(dialog).getAllByRole('status');
    await user.click(checkbox);
    const note = await within(dialog).findByRole('status', { name: /another plan holds/i });
    // a live region inserted together with its text is not reliably read
    expect(regions).toContain(note);
  });

  it('says in the form that the providers are unavailable, with Retry, instead of empty cloud lists (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    // a 4xx is not retried: the error shows at once
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/providers'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent(/providers are unavailable/i);
    // Retry fills the cloud lists once the providers load
    server.handle = handle;
    await user.click(within(alert).getByRole('button', { name: /retry/i }));
    const sources = server.providers.filter((p) => p.role === 'source');
    await waitFor(() => expect(within(within(dialog).getByLabelText(/source provider/i)).getAllByRole('option')).toHaveLength(sources.length + 1));
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    // the list reads no migration, so no page banner names a failed /migrations: only the form does (SDD §16)
    ['/migrations', null],
    ['/plans', /plans are unavailable/i],
  ])('says it cannot check whether another plan holds a selected VM when %s cannot be loaded (SDD §5.4, §16)', async (failing, pageBanner) => {
    const server = createTestServer();
    const holder = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === holder.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === failing
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), holder.source_provider_id);
    // the load has failed (the page behind names a failed plan list), but with no VM selected there is nothing to check
    if (pageBanner) await screen.findByText(pageBanner);
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    await user.click(await within(dialog).findByRole('checkbox', { name: `Select ${held.vm.name}` }));

    // no silent all clear: the form says the check could not be made
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent(/cannot check whether another plan holds the selected vms/i);
    expect(within(dialog).queryByRole('status', { name: /another plan holds/i })).not.toBeInTheDocument();
    // Retry runs the check: the holder is named again
    server.handle = handle;
    await user.click(within(alert).getByRole('button', { name: /retry/i }));
    const note = await within(dialog).findByRole('status', { name: /another plan holds/i });
    expect(note).toHaveTextContent(`${held.vm.name} (plan "${holder.name}", ${holder.id}, ${held.phase})`);
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });

  it("maps a plan's network cut-over: hooks per point, the floating IP move, DNS targets and Octavia pools", async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const at = '2026-10-08T12:00:00Z';
    for (const name of ['drain-lb', 'warm-cache', 'smoke'])
      server.hooks.set(name, { name, kind: 'playbook', description: null, playbook: `/opt/hooks/${name}.yml`, url: null, secret: null, timeout_s: 300, revert: null, created_at: at, updated_at: at });
    const web = (server as unknown as { inventories: Record<string, VMRef[]> }).inventories['rhosp17-dc1']!.find((vm) => vm.name === 'web-01')!;
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
    const dialog = await newPlan(user, 'Cut over the portal', 'rhosp17-dc1');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:.*network cut-over/i));
    const section = within(dialog).getByRole('group', { name: /^network cut-over/i });
    expect(within(section).getByText('Technology Preview')).toBeInTheDocument();
    await user.click(within(section).getByRole('radio', { name: /keep each address/i }));
    // each point's hooks in their run order: Add hook offers what the point does not run yet
    const addTo = async (point: RegExp, hook: string) => {
      const group = within(section).getByRole('group', { name: point });
      await user.selectOptions(within(group).getByLabelText(/^hook to add/i), hook);
      await user.click(within(group).getByRole('button', { name: /^add hook/i }));
      return group;
    };
    await addTo(/^before the stop/i, 'drain-lb');
    await addTo(/^after the server is created/i, 'warm-cache');
    const postCreate = await addTo(/^after the server is created/i, 'smoke');
    expect(within(within(postCreate).getByLabelText(/^hook to add/i)).queryByRole('option', { name: 'smoke' })).not.toBeInTheDocument();
    await user.click(within(postCreate).getByRole('button', { name: 'Move smoke up' }));
    expect(within(postCreate).getAllByRole('listitem').map((item) => item.getAttribute('aria-label'))).toEqual(['smoke', 'warm-cache']);
    // a DNS target of a selected VM, its server and TSIG secret asked for an RFC 2136 one
    await user.click(within(section).getByRole('button', { name: 'Add DNS target' }));
    await user.selectOptions(within(section).getByLabelText(/^DNS target 1 VM/), web.source_id);
    await user.selectOptions(within(section).getByLabelText(/^DNS target 1 kind/), 'rfc2136');
    await user.type(within(section).getByLabelText(/^DNS target 1 zone/), 'example.com.');
    await user.type(within(section).getByLabelText(/^DNS target 1 record/), 'web-01');
    await user.type(within(section).getByLabelText(/^DNS target 1 server/), 'ns1.example.com:53');
    await user.type(within(section).getByLabelText(/^DNS target 1 TSIG secret/), 'web-tsig');
    // an Octavia pool mapping
    await user.click(within(section).getByRole('button', { name: 'Add pool mapping' }));
    await user.type(within(section).getByLabelText(/^Pool mapping 1 source pool/), 'pool-src-1');
    await user.type(within(section).getByLabelText(/^Pool mapping 1 destination pool/), 'pool-dst-9');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Cut over the portal')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Cut over the portal')!;
    expect(created.network_cutover).toEqual({
      hooks: { pre_stop: ['drain-lb'], post_create: ['smoke', 'warm-cache'] },
      floating_ips: 'keep_address',
      dns_targets: { [web.source_id]: { kind: 'rfc2136', zone: 'example.com.', record: 'web-01', server: 'ns1.example.com:53', tsig_secret: 'web-tsig' } },
      lb_pools: { 'pool-src-1': 'pool-dst-9' },
    });
  });

  it("says why a point offers no hook, and refuses an incomplete DNS target or pool mapping (SDD §16, plan task N12b)", async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'No hooks yet', 'rhosp17-dc1');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:.*network cut-over/i));
    const section = within(dialog).getByRole('group', { name: /^network cut-over/i });
    expect(await within(section).findAllByText(/no hooks registered: an admin registers them on the/i)).toHaveLength(5);
    expect(within(section).getAllByRole('link', { name: 'Hooks page' })[0]).toHaveAttribute('href', '/hooks');
    await user.click(within(section).getByRole('button', { name: 'Add DNS target' }));
    await user.click(within(section).getByRole('button', { name: 'Add pool mapping' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    // in the error summary, which links to the section, and in the section itself
    expect(await within(dialog).findAllByText('DNS target 1: choose a VM, and enter its zone and record.')).toHaveLength(2);
    expect(within(dialog).getByRole('group', { name: /^network cut-over/i })).toHaveAccessibleDescription('DNS target 1: choose a VM, and enter its zone and record.');
  });

  it('lets a VM taken out of the plan take its DNS target with it (SDD §16, plan task N12b)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'Targets follow their VMs', 'rhosp17-dc1');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select web-02' }));
    await user.click(within(dialog).getByText(/^advanced:.*network cut-over/i));
    const section = within(dialog).getByRole('group', { name: /^network cut-over/i });
    for (const [n, vm] of [
      [1, 'os-0a11'],
      [2, 'os-0a12'],
    ] as const) {
      await user.click(within(section).getByRole('button', { name: 'Add DNS target' }));
      await user.selectOptions(within(section).getByLabelText(new RegExp(`^DNS target ${n} VM`)), vm);
      await user.type(within(section).getByLabelText(new RegExp(`^DNS target ${n} zone`)), 'example.com.');
    }
    await user.type(within(section).getByLabelText(/^DNS target 1 record/), 'record-1');
    // web-02 leaves the plan, and its target with it; back in the plan, its target is too, as its strategy override is
    const web02 = within(dialog).getByRole('checkbox', { name: 'Select web-02' });
    await user.click(web02);
    expect(within(section).queryByLabelText(/^DNS target 2 VM/)).not.toBeInTheDocument();
    await user.click(web02);
    expect(within(section).getByLabelText(/^DNS target 2 zone/)).toHaveValue('example.com.');
    // out again: its target, without a record yet, is not for the form to refuse, and only web-01's is sent
    await user.click(web02);
    expect(within(section).queryByLabelText(/^DNS target 2 VM/)).not.toBeInTheDocument();
    expect(within(section).getByLabelText(/^DNS target 1 VM/)).toHaveValue('os-0a11');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Targets follow their VMs')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Targets follow their VMs')!;
    expect(created.network_cutover.dns_targets).toEqual({ 'os-0a11': { kind: 'designate', zone: 'example.com.', record: 'record-1', server: null, tsig_secret: null } });
  });

  it('sets project mappings and every verification setting from the form (dashboard-first, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    const { field, enter } = formFields(user, dialog);
    await enter(/^name/i, 'Verified wave');
    await user.selectOptions(field(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(field(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await enter(/^project mappings/i, 'shop = shop-prod');
    await user.selectOptions(field(/^probe address/i), 'floating');
    await enter(/^verification timeout/i, '15');
    await enter(/^console success patterns/i, 'login:\nReached target .*Multi-User');
    await user.click(within(dialog).getByRole('checkbox', { name: /advisor reviews the verification/i }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Verified wave')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Verified wave')!;
    expect(created.mappings.projects).toEqual({ shop: 'shop-prod' });
    expect(created.verification).toMatchObject({
      probe_address: 'floating',
      timeout_s: 900,
      console_success_patterns: ['login:', 'Reached target .*Multi-User'],
      use_advisor: false,
    });
  });

  it('sets the keep-warm interval and the pre-staged resources of a new plan (dashboard-first, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Warm wave');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    const keepWarm = within(dialog).getByLabelText(/^keep-warm interval/i);
    expect(keepWarm).toHaveValue(15);
    await user.clear(keepWarm);
    await user.type(keepWarm, '30');
    const prestage = within(dialog).getByRole('group', { name: /pre-staged at the destination/i });
    expect(within(prestage).getAllByRole('checkbox')).toHaveLength(8);
    expect(within(prestage).getByRole('checkbox', { name: /^security group rules/i })).toBeChecked();
    // flavors and keypairs are opt-in (SDD §16)
    expect(within(prestage).getByRole('checkbox', { name: /^flavors/i })).not.toBeChecked();
    expect(within(prestage).getByRole('checkbox', { name: /^keypairs/i })).not.toBeChecked();
    await user.click(within(prestage).getByRole('checkbox', { name: /^security group rules/i }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Warm wave')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Warm wave')!;
    expect(created.keep_warm_interval_s).toBe(1800);
    expect(created.prestage_resources).toEqual(['networks', 'subnets', 'routers', 'router_interfaces', 'security_groups']);
  });

  it("pre-stages the plan's flavors RHOSO lacks when asked, after the defaults (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Flavors too');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    const prestage = within(dialog).getByRole('group', { name: /pre-staged at the destination/i });
    const flavors = within(prestage).getByRole('checkbox', { name: /^flavors/i });
    expect(flavors).toHaveAccessibleDescription(/flavors of the plan's vms that the destination lacks/i);
    await user.click(flavors);
    const keypairs = within(prestage).getByRole('checkbox', { name: /^keypairs/i });
    expect(keypairs).toHaveAccessibleDescription(/keypairs the migration user lacks on the destination/i);
    await user.click(keypairs);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Flavors too')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Flavors too')!.prestage_resources).toEqual([
      'networks', 'subnets', 'routers', 'router_interfaces', 'security_groups', 'security_group_rules', 'flavors', 'keypairs',
    ]);
  });

  it('sets a per-VM strategy override; a VM taken out of the plan loses its override (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Override wave');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select web-02' }));
    const overrides = within(dialog).getByRole('group', { name: /per-vm strategy/i });
    await user.selectOptions(within(overrides).getByLabelText(/^vm$/i), 'web-01');
    await user.selectOptions(within(overrides).getByLabelText(/^override strategy$/i), 'warm');
    await user.click(within(overrides).getByRole('button', { name: /^add override$/i }));
    await user.selectOptions(within(overrides).getByLabelText(/^vm$/i), 'web-02');
    await user.selectOptions(within(overrides).getByLabelText(/^override strategy$/i), 'cold');
    await user.click(within(overrides).getByRole('button', { name: /^add override$/i }));
    expect(within(overrides).getByLabelText(/^strategy for web-01$/i)).toHaveValue('warm');
    expect(within(overrides).getByLabelText(/^strategy for web-02$/i)).toHaveValue('cold');
    // web-02 leaves the plan: its override goes with it
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select web-02' }));
    expect(within(overrides).queryByLabelText(/^strategy for web-02$/i)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Override wave')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Override wave')!.strategy_overrides).toEqual({ 'os-0a11': 'warm' });
  });

  it('changes an override in place, and another source clears every override (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    const overrides = within(dialog).getByRole('group', { name: /per-vm strategy/i });
    const add = within(overrides).getByRole('button', { name: /^add override$/i });
    expect(add).toHaveAttribute('aria-disabled', 'true');
    await user.selectOptions(within(overrides).getByLabelText(/^vm$/i), 'web-01');
    await user.selectOptions(within(overrides).getByLabelText(/^override strategy$/i), 'warm');
    await user.click(add);
    await user.selectOptions(within(overrides).getByLabelText(/^strategy for web-01$/i), 'storage_handover');
    expect(within(overrides).getByLabelText(/^strategy for web-01$/i)).toHaveValue('storage_handover');
    // every selected VM has an override now: nothing left to choose
    expect(within(overrides).getByLabelText(/^vm$/i)).toBeDisabled();

    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'vcenter-hq');
    expect(within(overrides).queryByLabelText(/^strategy for /i)).not.toBeInTheDocument();
    expect(within(within(overrides).getByLabelText(/^override strategy$/i)).getAllByRole('option').map((o) => o.getAttribute('value'))).toEqual(['', 'vmware_cold', 'vmware_warm']);
  });

  it('caps the plan name at 200 characters and the description at 2000, like the API (SDD §12)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    expect(within(dialog).getByLabelText(/^name/i)).toHaveAttribute('maxlength', '200');
    expect(within(dialog).getByLabelText(/^description/i)).toHaveAttribute('maxlength', '2000');
  });

  it('refuses a keep-warm interval under a minute (SDD §5.4)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const keepWarm = within(dialog).getByLabelText(/^keep-warm interval/i);
    await user.clear(keepWarm);
    await user.type(keepWarm, '0.5');
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));

    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/keep-warm interval in minutes \(1 or more\)/i);
    expect(within(dialog).getByLabelText(/^keep-warm interval/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('refuses a finalize reminder that is not whole hours, 1 or more (SDD §4.2, ADV-19)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const reminder = within(dialog).getByLabelText(/^finalize reminder \(hours\)/i);
    expect(reminder).toHaveValue(72);
    for (const value of ['0', '1.5']) {
      await user.clear(reminder);
      await user.type(reminder, value);
      await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));
      const summary = await within(dialog).findByRole('alert');
      expect(summary).toHaveTextContent('Enter the finalize reminder in whole hours (1 or more).');
      expect(within(dialog).getByLabelText(/^finalize reminder \(hours\)/i)).toHaveAttribute('aria-invalid', 'true');
    }
  });

  it('keeps MAC addresses and every fixed IP when asked, off by default (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Ports kept');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    const ports = within(dialog).getByRole('group', { name: /^ports$/i });
    const keep = within(ports).getByRole('checkbox', { name: /^keep mac addresses and every fixed ip/i });
    expect(keep).not.toBeChecked();
    expect(keep).toHaveAccessibleDescription(/through neutron with its mac address, all its fixed ips and its security groups/i);
    expect(keep).toHaveAccessibleDescription(/outlive their server/i);
    expect(keep).toHaveAccessibleDescription(/cold, warm and storage handover/i);
    await user.click(keep);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Ports kept')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Ports kept')!.port_creation_mode).toBe('neutron');
  });

  it('says a VMware source pre-stages nothing (SDD §7.2)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'vcenter-hq');
    await user.click(within(dialog).getByText(/^advanced:/i));
    const prestage = within(dialog).getByRole('group', { name: /pre-staged at the destination/i });
    expect(prestage).toHaveTextContent(/vmware sources pre-stage nothing/i);
    for (const box of within(prestage).getAllByRole('checkbox')) expect(box).toBeDisabled();
    // the migration kit recreates the NICs (SDD §16)
    const keep = within(within(dialog).getByRole('group', { name: /^ports$/i })).getByRole('checkbox', { name: /^keep mac addresses/i });
    expect(keep).toBeDisabled();
    expect(keep).toHaveAccessibleDescription(/vmware sources ignore this/i);
  });

  it('refuses a negative verification timeout and a malformed project mapping, and opens the advanced section', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByText(/^advanced:/i));
    const timeout = within(dialog).getByLabelText(/^verification timeout/i);
    await user.clear(timeout);
    await user.type(timeout, '-1');
    await user.type(within(dialog).getByLabelText(/^project mappings/i), 'no arrow here');
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));

    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/verification timeout in minutes \(0 or more\)/i);
    expect(within(dialog).getByLabelText(/^verification timeout/i)).toHaveAttribute('aria-invalid', 'true');
    expect(within(dialog).getByLabelText(/^project mappings/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('turns on storage handover with a RHOSO backend per volume type (Ceph and NetApp)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Handover test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select report-gen-01' }));
    await user.click(within(dialog).getByText(/^advanced/i));
    await user.click(within(dialog).getByRole('checkbox', { name: /hand volumes over without copying/i }));

    // the NetApp type has one compatible backend; the pool is resolved per volume
    const netapp = within(dialog).getByLabelText(/netapp-nfs/i);
    expect(netapp).toHaveValue('hostgroup@ontap-nfs');
    expect(within(dialog).getByText(/198\.51\.100\.60:\/cinder_dc1/)).toBeInTheDocument();
    // the Ceph type has three RBD pools: the operator picks one
    const ceph = within(dialog).getByLabelText(/^tripleo-ceph/i);
    expect(ceph).toHaveValue('');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 2 vms/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/choose a destination backend for tripleo-ceph/i);

    await user.selectOptions(ceph, 'hostgroup@ceph-ssd#volumes-ssd');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 2 vms/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Handover test')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Handover test')?.handover).toEqual({
      enabled: true,
      backend_map: { 'netapp-nfs': 'hostgroup@ontap-nfs', 'tripleo-ceph': 'hostgroup@ceph-ssd#volumes-ssd' },
    });
  });

  it.each(['constructor', 'toString', '__proto__'])(
    'maps a volume type named %s to its one compatible backend, as any other type (SDD §7.3.1, §16, review RV24-01)',
    async (type) => {
      const user = userEvent.setup({ delay: null });
      const server = createTestServer();
      withVolumeType(server, 'os-r24a', type, 'overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1');
      renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator', server });
      await user.click(await screen.findByRole('button', { name: /new plan/i }));
      const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
      await user.type(within(dialog).getByLabelText(/^name/i), 'Odd type');
      await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
      await user.selectOptions(within(dialog).getByLabelText(/destination provider/i), 'rhoso-prod');
      await user.click(await within(dialog).findByRole('checkbox', { name: 'Select odd-01' }));
      await user.click(within(dialog).getByText(/^advanced/i));
      await user.click(within(dialog).getByRole('checkbox', { name: /hand volumes over without copying/i }));
      // the type's name is read by own key, as the API's dict: its one compatible backend, not an inherited member
      expect(within(dialog).getByLabelText(new RegExp(`^${type}`))).toHaveValue('hostgroup@ontap-nfs');
      await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
      await waitFor(() => expect(server.plans.some((p) => p.name === 'Odd type')).toBe(true));
      expect(Object.entries(server.plans.find((p) => p.name === 'Odd type')!.handover.backend_map)).toEqual([[type, 'hostgroup@ontap-nfs']]);
    },
  );

  it('asks before Close discards a changed form, and each opening starts afresh (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    // nothing changed: Close discards nothing, so it closes at once
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: /new migration plan/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    // a name typed: Close asks first
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Half done');
    await user.keyboard('{Escape}');
    const ask = await screen.findByRole('alertdialog', { name: /discard unsaved plan changes/i });
    await user.click(within(ask).getByRole('button', { name: 'Discard changes' }));
    expect(screen.queryByRole('dialog', { name: /new migration plan/i })).not.toBeInTheDocument();
    // opened again: the discarded name is gone, and the fresh form counts as unchanged
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    const fresh = await screen.findByRole('dialog', { name: /new migration plan/i });
    expect(within(fresh).getByLabelText(/^name/i)).toHaveValue('');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: /new migration plan/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('opens the new-plan dialog with the VMs chosen in the inventory, and closed gives the focus to the page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(
      <Routes>
        <Route element={<Layout />}>
          <Route path="/inventory/:providerId" element={<Inventory />} />
          <Route path="/plans" element={<Plans />} />
        </Route>
      </Routes>,
      { route: '/inventory/rhosp17-dc1', token: 'operator' },
    );
    const table = await screen.findByRole('table', { name: /^VMs on/ });
    await user.click(within(table).getByRole('checkbox', { name: 'Select web-01' }));
    await user.click(screen.getByRole('button', { name: /^create plan with 1 vm$/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    // the selection comes with it, and the form has the focus: the move to the new page came first
    expect(within(dialog).getByLabelText(/source provider/i)).toHaveValue('rhosp17-dc1');
    expect(await within(dialog).findByRole('checkbox', { name: 'Select web-01' })).toBeChecked();
    await waitFor(() => expect(within(dialog).getByLabelText(/^name/i)).toHaveFocus());
    // closed unchanged, the focus goes back where the new page put it: its main region
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: /new migration plan/i })).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveFocus();
  });
});

describe('changed-extent tracking in the plan form (SDD §16, §28.9, plan task CE10)', () => {
  it('offers extent tracking for an OpenStack source only, with the Technology Preview badge', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'Track the finance disks', 'vcenter-hq');
    await user.click(within(dialog).getByText(/^advanced:/i));
    // a VMware source has no Ceph cluster: the section is not offered (SDD §28.9)
    expect(within(dialog).queryByRole('group', { name: /^changed-extent tracking/i })).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    const section = within(dialog).getByRole('group', { name: /^changed-extent tracking/i });
    expect(within(section).getByText('Technology Preview')).toBeInTheDocument();
    const toggle = within(section).getByRole('checkbox', { name: /track changed extents on the source ceph cluster/i });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/needs the source provider's ceph access and key/i);
    expect(within(dialog).getByText(/^advanced:/i)).not.toHaveTextContent(/changed-extent tracking/i);
    await user.click(toggle);
    expect(within(dialog).getByText(/^advanced:/i)).toHaveTextContent(/changed-extent tracking/i);
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Track the finance disks')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Track the finance disks')!.extent_tracking).toBe(true);
  });
});

describe("Plans: the plan's project pair (SDD §16, §27.4; plan task TC8)", () => {
  // the mock cloud's projects the RHOSP 17.1 source holds credentials for (mockData MOCK_CLOUD_PROJECTS)
  const SHOP = '5f0c3a9e2b7d41c8a6e9d2f1b3c4a5e6';
  const PLATFORM = '8d2e6b1f4a9c47e3b5d0c8a7f6e1d2c3';
  // and the RHOSO destination's of the same names, other ids, as the demo seeds them (SDD §10, plan task TC9)
  const DST_SHOP = '2a7c9e1b3d5f47a8b0c2e4d6f8a1b3c5';
  const DST_PLATFORM = '7f1e3d5c9b2a46e8a0d4c6b8e2f1a3d5';
  const optionsOf = (select: HTMLElement) => within(select).getAllByRole('option').map((o) => [(o as HTMLOptionElement).value, o.textContent]);

  it("offers the provider's own project and its credentials' projects for the plan's project pair", async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'Shop tenant', 'rhosp17-dc1');
    const source = within(dialog).getByRole('combobox', { name: /^source project/i });
    const destination = within(dialog).getByRole('combobox', { name: /^destination project/i });
    // its own credential's project first, sent as none; then its credentials' projects by name (SDD §16)
    expect(optionsOf(source)).toEqual([
      ['', "Its own credential's project: finance (project-finance)"],
      [PLATFORM, `platform (${PLATFORM})`],
      [SHOP, `shop (${SHOP})`],
    ]);
    expect(optionsOf(destination)).toEqual([
      ['', "Its own credential's project: demo (project-demo)"],
      [DST_PLATFORM, `platform (${DST_PLATFORM})`],
      [DST_SHOP, `shop (${DST_SHOP})`],
    ]);
    expect((source as HTMLSelectElement).value).toBe('');
    // with the Technology Preview badge (SDD §16, §27.10)
    expect(within(source.closest('[role="group"]') as HTMLElement).getByText('Technology Preview')).toBeInTheDocument();
    // another source takes that side back to its own project
    await user.selectOptions(source, SHOP);
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'kolla-edge');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    expect((within(dialog).getByRole('combobox', { name: /^source project/i }) as HTMLSelectElement).value).toBe('');
    await user.selectOptions(within(dialog).getByRole('combobox', { name: /^source project/i }), SHOP);
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Shop tenant')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Shop tenant')!;
    expect(created).toMatchObject({ source_project: SHOP, destination_project: null });
  });

  it("offers the VMs of the plan's source project, and clears the selection when it changes", async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'Platform tenant', 'rhosp17-dc1');
    // its own credential reads the VMs it lists, a shop VM and a platform VM among them
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    expect(within(dialog).getByRole('checkbox', { name: 'Select mq-broker-01' })).toBeInTheDocument();
    // the platform project's credential reads its own VMs alone, and the selection goes with the old project (SDD §16)
    await user.selectOptions(within(dialog).getByRole('combobox', { name: /^source project/i }), PLATFORM);
    expect(await within(dialog).findByRole('checkbox', { name: 'Select mq-broker-01' })).not.toBeChecked();
    expect(within(dialog).queryByRole('checkbox', { name: 'Select web-01' })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('checkbox', { name: 'Select mq-broker-01' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Platform tenant')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Platform tenant')).toMatchObject({ source_project: PLATFORM, vm_ids: ['os-0b31'] });
  });

  it('offers no project for a VMware source', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const dialog = await newPlan(user, 'From vCenter', 'vcenter-hq');
    expect(within(dialog).queryByRole('combobox', { name: /^source project/i })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('combobox', { name: /^destination project/i })).not.toBeInTheDocument();
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select vm-erp-app-01' }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'From vCenter')).toBe(true));
    // both sent none: a VMware plan names no project (SDD §27.9)
    expect(server.plans.find((p) => p.name === 'From vCenter')).toMatchObject({ source_project: null, destination_project: null });
  });
});
