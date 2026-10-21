import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { buildInventories, makeVm } from '../api/mockData';
import type { VMRef } from '../api/types';
import { gpuDevice, gpuRequests } from '../lib/gpu';
import { VmTable } from './VmTable';

const inventories = buildInventories();
const openstackVms = inventories['rhosp17-dc1'] as VMRef[];
const vmwareVms = inventories['vcenter-hq'] as VMRef[];

function bodyRows(): HTMLElement[] {
  const table = screen.getByRole('table');
  return within(table).getAllByRole('row').slice(1);
}

function rowNames(): string[] {
  return bodyRows().map((row) => within(row).getAllByRole('rowheader')[0]?.textContent ?? '');
}

describe('VmTable', () => {
  it('lists every VM with its size and a live result count', () => {
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    expect(bodyRows()).toHaveLength(openstackVms.length);
    expect(screen.getByRole('status')).toHaveTextContent(`Showing ${openstackVms.length} of ${openstackVms.length} VMs`);
    const oracle = bodyRows().find((row) => row.textContent?.includes('db-oracle-03'));
    expect(oracle).toBeDefined();
    // 100 GiB + 1200 GiB = 1300 GiB, shown in TiB.
    expect(oracle).toHaveTextContent('1.3 TiB');
  });

  it('filters by free text across name, project, OS and tags', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);

    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'db-');
    expect(rowNames().sort()).toEqual(['db-mysql-02', 'db-oracle-03', 'db-pg-01']);
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 3 of ${openstackVms.length} VMs`);

    await user.clear(screen.getByRole('searchbox', { name: /search vms/i }));
    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'ANALYTICS');
    expect(rowNames()).toHaveLength(4);
  });

  it('filters by power state, project and readiness', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);

    await user.selectOptions(screen.getByLabelText('Power state'), 'stopped');
    expect(rowNames()).toEqual(['old-ftp-01']);

    await user.selectOptions(screen.getByLabelText('Power state'), 'all');
    await user.selectOptions(screen.getByLabelText('Readiness'), 'blocker');
    expect(rowNames().sort()).toEqual(['build-runner-07']);
    // a GPU VM needs a GPU mapping in its plan: guidance, which its plan decides (SDD §22.4, §22.11)
    await user.selectOptions(screen.getByLabelText('Readiness'), 'attention');
    expect(rowNames()).toContain('gpu-render-01');

    await user.selectOptions(screen.getByLabelText('Readiness'), 'all');
    await user.selectOptions(screen.getByLabelText('Project'), 'analytics');
    expect(rowNames()).toHaveLength(4);
  });

  it('shows an empty state with a way back when nothing matches', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);

    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'web');
    await user.selectOptions(screen.getByLabelText('Power state'), 'stopped');
    expect(screen.getByText('No VMs match these filters')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(bodyRows()).toHaveLength(openstackVms.length);
  });

  it('keeps the focus in the filters when Clear filters goes away (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    const search = screen.getByRole('searchbox', { name: /search vms/i });
    // the toolbar's Clear filters
    await user.selectOptions(screen.getByLabelText('Guest OS'), 'windows');
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(search).toHaveFocus();
    // the empty state's Clear filters
    await user.type(search, 'no-such-vm');
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(search).toHaveFocus();
  });

  it('shows readiness flags as text, including VMware CBT state', () => {
    render(<VmTable vms={vmwareVms} providerKind="vmware" />);
    const row = (name: string) => bodyRows().find((r) => r.textContent?.includes(name));
    expect(row('vm-fileserver-01')).toHaveTextContent('CBT off');
    expect(row('vm-print-01')).toHaveTextContent('Independent disk');
    expect(row('vm-print-01')).toHaveTextContent('Tools missing');
    expect(row('vm-hr-portal')).toHaveTextContent('2 snapshots');
    expect(row('vm-erp-app-01')).toHaveTextContent('Ready');
  });

  it('gives a readiness flag its guidance on hover, focus and tap, never only in a title (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    const row = bodyRows().find((r) => r.textContent?.includes('db-pg-01'))!;
    const flag = within(row).getByRole('button', { name: 'Encrypted volume' });
    expect(flag).toHaveAccessibleDescription('VOL_ENCRYPTED: The Barbican key must be re-created on the destination.');
    expect(within(row).queryByTitle(/VOL_ENCRYPTED/)).not.toBeInTheDocument();
    // no truncating ancestor: its overflow would clip the focus ring (WCAG 2.4.7)
    expect(flag.closest('.truncate')).toBeNull();
    // a tap shows it too: touch screens have no hover
    await user.click(flag);
    expect(screen.getByRole('tooltip')).toHaveTextContent('VOL_ENCRYPTED: The Barbican key must be re-created on the destination.');
  });

  it('sorts by disk size with aria-sort', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    const diskHeader = screen.getByRole('columnheader', { name: /disks/i });

    await user.click(within(diskHeader).getByRole('button'));
    expect(diskHeader).toHaveAttribute('aria-sort', 'ascending');
    await user.click(within(diskHeader).getByRole('button'));
    expect(diskHeader).toHaveAttribute('aria-sort', 'descending');
    expect(rowNames()[0]).toBe('db-oracle-03');
  });

  it('supports selecting VMs for a plan', async () => {
    const user = userEvent.setup({ delay: null });
    const onSelectedChange = vi.fn();
    render(
      <VmTable vms={openstackVms} providerKind="openstack" selected={new Set(['os-0a11'])} onSelectedChange={onSelectedChange} />,
    );

    expect(screen.getByRole('checkbox', { name: 'Select web-01' })).toBeChecked();
    await user.click(screen.getByRole('checkbox', { name: 'Select web-02' }));
    expect(onSelectedChange).toHaveBeenLastCalledWith(new Set(['os-0a11', 'os-0a12']));

    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'db-');
    await user.click(screen.getByRole('checkbox', { name: /select all 3 shown/i }));
    expect(onSelectedChange).toHaveBeenLastCalledWith(new Set(['os-0a11', 'os-0d51', 'os-0d52', 'os-0d53']));
  });

  it('says the header checkbox selects every matching VM, also those not shown yet', async () => {
    const user = userEvent.setup({ delay: null });
    const onSelectedChange = vi.fn();
    const table = (selected: Set<string>) => (
      <VmTable vms={openstackVms} providerKind="openstack" selected={selected} onSelectedChange={onSelectedChange} pageSize={2} />
    );
    const { rerender } = render(table(new Set()));
    const total = openstackVms.length;
    // two rows are rendered of all the VMs: the status and the checkbox say so
    expect(bodyRows()).toHaveLength(2);
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 2 of ${total} VMs, 0 selected`);
    await user.click(screen.getByRole('checkbox', { name: `Select all ${total} matching VMs, 2 shown` }));
    const chosen = onSelectedChange.mock.lastCall?.[0] as Set<string>;
    expect(chosen.size).toBe(total);
    rerender(table(chosen));
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 2 of ${total} VMs, ${total} selected (${total - 2} not shown)`);

    // with a filter, the counts are those of the matching VMs (three db- VMs, two of them shown)
    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'db-');
    expect(screen.getByRole('status')).toHaveTextContent(`Showing 2 of 3 matching VMs (${total} in total)`);
    expect(screen.getByRole('checkbox', { name: 'Select all 3 matching VMs, 2 shown' })).toBeInTheDocument();
  });

  it('moves focus to the table when its last Show more button goes, never to the page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" pageSize={2} />);
    let clicks = 0;
    for (let more = screen.queryByRole('button', { name: /^show \d+ more$/i }); more; more = screen.queryByRole('button', { name: /^show \d+ more$/i })) {
      await user.click(more);
      clicks += 1;
      const still = screen.queryByRole('button', { name: /^show \d+ more$/i });
      // while the button stays, it keeps the focus; once it goes, the table takes it
      expect(still ?? screen.getByRole('table')).toHaveFocus();
    }
    expect(clicks).toBeGreaterThan(1);
    expect(bodyRows()).toHaveLength(openstackVms.length);
  });

  it('names each guest OS, flags legacy releases and filters by OS', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    const row = (name: string) => bodyRows().find((r) => within(r).getAllByRole('rowheader')[0]?.textContent === name)!;
    expect(row('web-02')).toHaveTextContent('Ubuntu 22.04');
    expect(row('report-gen-01')).toHaveTextContent('Windows Server 2022');
    // legacy releases carry the readiness hint from the catalog (SDD §9.5)
    expect(within(row('jump-host-01')).getByRole('button', { name: 'Legacy OS' })).toHaveAccessibleDescription(/^GUEST_OS_LEGACY: Ubuntu 18\.04/);
    expect(within(row('legacy-rhel6-app')).getByRole('button', { name: 'Legacy OS' })).toHaveAccessibleDescription(/^GUEST_OS_LEGACY: RHEL 6/);

    await user.selectOptions(screen.getByLabelText('Guest OS'), 'windows');
    expect(rowNames().sort()).toEqual(['ad-dc-01', 'report-gen-01']);
    await user.selectOptions(screen.getByLabelText('Guest OS'), 'legacy');
    expect(rowNames().sort()).toEqual(['jump-host-01', 'legacy-rhel6-app']);
  });

  it('clears the guest OS filter with the other filters', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);

    // the OS filter alone is an active filter: the way back is offered and resets it
    await user.selectOptions(screen.getByLabelText('Guest OS'), 'windows');
    expect(bodyRows()).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Guest OS')).toHaveValue('all');
    expect(bodyRows()).toHaveLength(openstackVms.length);

    // the empty state's Clear filters resets it too, instead of leaving the table empty
    await user.selectOptions(screen.getByLabelText('Guest OS'), 'windows');
    await user.type(screen.getByRole('searchbox', { name: /search vms/i }), 'web');
    expect(screen.getByText('No VMs match these filters')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(bodyRows()).toHaveLength(openstackVms.length);
  });

  describe("a VM's GPUs (SDD §16, §22.11)", () => {
    /** The GPU column's text for a VM: the cell under the GPU header. */
    const gpuOf = (name: string): string | null => {
      const column = screen.getAllByRole('columnheader').indexOf(screen.getByRole('columnheader', { name: /^GPU/ }));
      const row = bodyRows().find((r) => within(r).getByRole('rowheader').textContent === name)!;
      return [within(row).getByRole('rowheader'), ...within(row).getAllByRole('cell')][column]!.textContent;
    };

    it("shows each VM's GPUs in a GPU column and filters by GPU", async () => {
      const user = userEvent.setup({ delay: null });
      // two identical vGPUs are one entry with its count (§22.2), and a passthrough device may have no ids
      const twice = makeVm({ id: 'vm-6001', name: 'dc2-ml-03', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 80, kind: 'vmdk', volume_type: null }], gpus: [gpuDevice({ kind: 'vgpu', profile: 'grid_a100-4c', count: 2 })] });
      const noIds = makeVm({ id: 'vm-6002', name: 'dc2-dvx-01', disks: [{ size_gb: 40, kind: 'vmdk', volume_type: null }], gpus: [gpuDevice({ kind: 'passthrough' })] });
      const vms = [...vmwareVms, twice, noIds];
      render(<VmTable vms={vms} providerKind="vmware" />);
      // the column says Technology Preview in words, its detail on hover, focus and tap
      const header = screen.getByRole('columnheader', { name: /^GPU/ });
      expect(within(header).getByRole('button', { name: 'Technology Preview' })).toHaveAccessibleDescription(
        'Technology Preview, validated in simulation: GPU migration has not yet run on GPU hosts.',
      );
      expect(gpuOf('dc2-ml-01')).toBe('vGPU grid_a100-4c');
      expect(gpuOf('dc2-ml-02')).toBe('vGPU grid_a100-3-20c');
      expect(gpuOf('dc2-cuda-01')).toBe('Passthrough 10de:20b5');
      expect(gpuOf('dc2-ml-03')).toBe('vGPU grid_a100-4c ×2');
      expect(gpuOf('dc2-dvx-01')).toBe('Passthrough without device ids');
      // empty for a VM without a GPU
      expect(gpuOf('vm-erp-app-01')).toBe('');

      // the GPU filter: those with a GPU, those without, and back to all with Clear filters
      const gpuVms = ['dc2-cuda-01', 'dc2-dvx-01', 'dc2-ml-01', 'dc2-ml-02', 'dc2-ml-03'];
      await user.selectOptions(screen.getByLabelText('GPU'), 'With a GPU');
      expect(rowNames().sort()).toEqual(gpuVms);
      expect(screen.getByRole('status')).toHaveTextContent(`Showing 5 of ${vms.length} VMs`);
      await user.selectOptions(screen.getByLabelText('GPU'), 'Without a GPU');
      expect(rowNames().sort()).toEqual(vms.map((vm) => vm.name).filter((name) => !gpuVms.includes(name)).sort());
      await user.click(screen.getByRole('button', { name: 'Clear filters' }));
      expect(screen.getByLabelText('GPU')).toHaveValue('all');
      expect(rowNames()).toHaveLength(vms.length);
    });

    it("words an OpenStack VM's GPU by what its flavor asks for, a VM recorded before 0.3.0 by its flavor's requests", () => {
      // a source that declares its flavor's profile reports it on the request: it stands after the kind (SDD §16, §22.2)
      const declared = makeVm({
        id: 'os-6003',
        name: 'gpu-ml-02',
        flavor: 'g1.vgpu',
        disks: [{ size_gb: 40 }],
        gpus: gpuRequests({ 'resources:VGPU': '2', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' }, 'A100-4C'),
      });
      render(<VmTable vms={[...openstackVms, declared]} providerKind="openstack" />);
      expect(gpuOf('gpu-ml-01')).toBe('vGPU · VGPU, CUSTOM_NVIDIA_A100_4C');
      expect(gpuOf('gpu-ml-02')).toBe('vGPU A100-4C ×2 · VGPU, CUSTOM_NVIDIA_A100_4C');
      // no gpus reported: its flavor's extra specs, read as the API's vm_gpus reads them (§22.2)
      expect(gpuOf('gpu-render-01')).toBe('vGPU · VGPU');
      expect(gpuOf('web-01')).toBe('');
    });

    it('gives the GPU filter a Technology Preview badge of its own, shown at every width as the GPU column is not (SDD §16)', () => {
      render(<VmTable vms={vmwareVms} providerKind="vmware" />);
      const filter = screen.getByLabelText('GPU');
      const badges = screen.getAllByRole('button', { name: 'Technology Preview' });
      const own = badges.find((badge) => !badge.closest('table'));
      // beside the filter, a focusable term with its definition, never inside an element a narrow screen hides
      expect(own).toBeDefined();
      expect(filter.parentElement).toContainElement(own!);
      expect(own).toHaveAccessibleDescription('Technology Preview, validated in simulation: GPU migration has not yet run on GPU hosts.');
      expect(own!.closest('.hidden')).toBeNull();
      // the filter keeps its name: the badge stands outside its label
      expect(filter).toHaveAccessibleName('GPU');
      // the column's badge goes with the column, which a narrow screen hides
      expect(badges.filter((badge) => badge !== own).map((badge) => badge.closest('th')?.className)).toEqual(['hidden md:table-cell']);
    });

    it('offers no GPU column or filter while no VM of the table has a GPU, as the Project filter needs two projects', () => {
      render(<VmTable vms={vmwareVms.filter((vm) => !vm.gpus?.length)} providerKind="vmware" />);
      expect(screen.queryByRole('columnheader', { name: /^GPU/ })).not.toBeInTheDocument();
      expect(screen.queryByLabelText('GPU')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Technology Preview' })).not.toBeInTheDocument();
    });
  });

  it('warns about guests virt-v2v does not support on VMware sources', () => {
    render(<VmTable vms={vmwareVms} providerKind="vmware" />);
    const legacy = bodyRows().find((r) => r.textContent?.includes('vm-legacy-win2008'))!;
    expect(within(legacy).getByRole('button', { name: 'Driver prep needed' })).toHaveAccessibleDescription(/^GUEST_CONVERSION_UNSUPPORTED: /);
    const ubuntu = bodyRows().find((r) => r.textContent?.includes('vm-hr-portal'))!;
    expect(ubuntu).toHaveTextContent('Ubuntu 24.04');
    expect(within(ubuntu).getByRole('button', { name: 'Test conversion' })).toHaveAccessibleDescription(/^GUEST_CONVERSION_UNVERIFIED: /);
  });

  describe('the plans that hold VMs (SDD §5.4, §16)', () => {
    const held = openstackVms[0]!;
    const holders = new Map([[held.source_id, [{ planId: 'plan-1', planName: 'Finance apps', phase: 'precopy' as const }]]]);
    const inRouter = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>);

    it('names the holding plan in the readiness cell, linked, with its phase; the row header stays the name', () => {
      inRouter(<VmTable vms={openstackVms} providerKind="openstack" holders={holders} />);
      const row = bodyRows().find((r) => within(r).getByRole('rowheader').textContent === held.name)!;
      expect(within(row).getByRole('link', { name: 'Finance apps' })).toHaveAttribute('href', '/plans/plan-1');
      // the phase is named as a phase: never a second 'Ready' beside the readiness one
      expect(row).toHaveTextContent('In plan Finance apps, phase Pre-copy');
      expect(within(row).getByRole('rowheader')).toHaveTextContent(new RegExp(`^${held.name}$`));
      // the free ones name no plan
      expect(bodyRows().filter((r) => r.textContent?.includes('In plan'))).toHaveLength(1);
    });

    it('filters the VMs no plan holds, or those one holds; Clear filters resets it', async () => {
      const user = userEvent.setup({ delay: null });
      inRouter(<VmTable vms={openstackVms} providerKind="openstack" holders={holders} />);
      await user.selectOptions(screen.getByLabelText('Plan'), 'Not in a plan');
      expect(rowNames()).not.toContain(held.name);
      expect(rowNames()).toHaveLength(openstackVms.length - 1);
      await user.selectOptions(screen.getByLabelText('Plan'), 'In a plan');
      expect(rowNames()).toEqual([held.name]);
      await user.click(screen.getByRole('button', { name: 'Clear filters' }));
      expect(rowNames()).toHaveLength(openstackVms.length);
    });

    it('says it does not know which plans hold the VMs while that is unknown, and offers no Plan filter', () => {
      inRouter(<VmTable vms={openstackVms} providerKind="openstack" holders={null} />);
      expect(screen.getByText('Which plans hold these VMs is unknown: the plans or their migrations could not be loaded.')).toBeInTheDocument();
      expect(screen.queryByLabelText('Plan')).not.toBeInTheDocument();
    });

    it('offers no Plan filter where holders are not tracked', () => {
      render(<VmTable vms={openstackVms} providerKind="openstack" />);
      expect(screen.queryByLabelText('Plan')).not.toBeInTheDocument();
    });
  });
});
