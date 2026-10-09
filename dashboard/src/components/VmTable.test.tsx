import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { buildInventories } from '../api/mockData';
import type { VMRef } from '../api/types';
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
    expect(rowNames().sort()).toEqual(['build-runner-07', 'gpu-render-01']);

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

  it('shows readiness flags as text, including VMware CBT state', () => {
    render(<VmTable vms={vmwareVms} providerKind="vmware" />);
    const row = (name: string) => bodyRows().find((r) => r.textContent?.includes(name));
    expect(row('vm-fileserver-01')).toHaveTextContent('CBT off');
    expect(row('vm-print-01')).toHaveTextContent('Independent disk');
    expect(row('vm-print-01')).toHaveTextContent('Tools missing');
    expect(row('vm-hr-portal')).toHaveTextContent('2 snapshots');
    expect(row('vm-erp-app-01')).toHaveTextContent('Ready');
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

  it('names each guest OS, flags legacy releases and filters by OS', async () => {
    const user = userEvent.setup({ delay: null });
    render(<VmTable vms={openstackVms} providerKind="openstack" />);
    const row = (name: string) => bodyRows().find((r) => within(r).getAllByRole('rowheader')[0]?.textContent === name)!;
    expect(row('web-02')).toHaveTextContent('Ubuntu 22.04');
    expect(row('report-gen-01')).toHaveTextContent('Windows Server 2022');
    // legacy releases carry the readiness hint from the catalog (SDD §9.5)
    expect(within(row('jump-host-01')).getByTitle(/GUEST_OS_LEGACY: Ubuntu 18\.04/)).toBeInTheDocument();
    expect(within(row('legacy-rhel6-app')).getByTitle(/GUEST_OS_LEGACY: RHEL 6/)).toBeInTheDocument();

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

  it('warns about guests virt-v2v does not support on VMware sources', () => {
    render(<VmTable vms={vmwareVms} providerKind="vmware" />);
    const legacy = bodyRows().find((r) => r.textContent?.includes('vm-legacy-win2008'))!;
    expect(within(legacy).getByTitle(/GUEST_CONVERSION_UNSUPPORTED/)).toBeInTheDocument();
    const ubuntu = bodyRows().find((r) => r.textContent?.includes('vm-hr-portal'))!;
    expect(ubuntu).toHaveTextContent('Ubuntu 24.04');
    expect(within(ubuntu).getByTitle(/GUEST_CONVERSION_UNVERIFIED/)).toBeInTheDocument();
  });
});
