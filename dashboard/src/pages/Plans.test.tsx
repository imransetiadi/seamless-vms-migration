import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import Plans from './Plans';

describe('Plans page', () => {
  it('lists plans with status in text', async () => {
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'viewer' });
    const table = await screen.findByRole('table', { name: /migration plans/i });
    expect(within(table).getAllByRole('row')).toHaveLength(server.plans.length + 1);
    expect(within(table).getByText('Paused')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new plan/i })).toHaveAttribute('aria-disabled', 'true');
  });

  it('says migration progress is unavailable instead of showing every plan with nothing done', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/migrations'
        ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Plans />, { server });

    // a 5xx is retried twice with backoff before the query reports the error
    expect(await screen.findByText(/migration progress is unavailable/i, {}, { timeout: 8000 })).toBeInTheDocument();
    // the Done column says it does not know, rather than showing every plan at 0 done
    const table = screen.getByRole('table');
    expect(within(table).queryByText(/^0\/\d+$/)).not.toBeInTheDocument();
    expect(within(table).getAllByText('unknown').length).toBeGreaterThan(0);
  }, 15_000); // the 5xx retries take about 3 s

  it('summarises validation errors and moves focus to the summary', async () => {
    const user = userEvent.setup();
    renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));

    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent(/fix 4 problems/i);
    await waitFor(() => expect(summary).toHaveFocus());
    expect(within(dialog).getByLabelText(/^name/i)).toHaveAttribute('aria-invalid', 'true');
  });

  it('creates a draft plan with the selected VMs', async () => {
    const user = userEvent.setup();
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Wave test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
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

  it('turns on storage handover with a RHOSO backend per volume type (Ceph and NetApp)', async () => {
    const user = userEvent.setup();
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Handover test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
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
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/choose a rhoso backend for tripleo-ceph/i);

    await user.selectOptions(ceph, 'hostgroup@ceph-ssd#volumes-ssd');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 2 vms/i }));
    await waitFor(() => expect(server.plans.some((p) => p.name === 'Handover test')).toBe(true));
    expect(server.plans.find((p) => p.name === 'Handover test')?.handover).toEqual({
      enabled: true,
      backend_map: { 'netapp-nfs': 'hostgroup@ontap-nfs', 'tripleo-ceph': 'hostgroup@ceph-ssd#volumes-ssd' },
    });
  });
});
