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

  it('creates a draft plan with the selected VMs', async () => {
    const user = userEvent.setup({ delay: null });
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

  it('sets the guest write rate and the aggregate scan cap of a new plan (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Write rate test');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
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

  it('refuses a guest write rate or aggregate scan cap that is not more than 0, like the API (SDD §9.1)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });
    const before = server.plans.length;
    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Bad rates');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await user.type(within(dialog).getByLabelText(/guest write rate/i), '0');
    await user.type(within(dialog).getByLabelText(/aggregate scan cap/i), '-5');
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));
    const summary = await within(dialog).findByRole('alert');
    expect(summary).toHaveTextContent('Enter the guest write rate in MiB/s (more than 0), or leave it empty.');
    expect(summary).toHaveTextContent('Enter the aggregate scan cap in MiB/s (more than 0), or leave it empty.');
    expect(server.plans).toHaveLength(before);
  });

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
    expect(note).toHaveTextContent(`${held.vm.name} (plan "${holder.name}", ${held.phase})`);
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
    ['/migrations', /migration progress is unavailable/i],
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
    // the load has failed (the page behind names it), but with no VM selected there is nothing to check
    await screen.findByText(pageBanner);
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
    expect(note).toHaveTextContent(`${held.vm.name} (plan "${holder.name}", ${held.phase})`);
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('sets project mappings and every verification setting from the form (dashboard-first, SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Verified wave');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    await user.type(within(dialog).getByLabelText(/^project mappings/i), 'shop = shop-prod');
    await user.selectOptions(within(dialog).getByLabelText(/^probe address/i), 'floating');
    const timeout = within(dialog).getByLabelText(/^verification timeout/i);
    await user.clear(timeout);
    await user.type(timeout, '15');
    const patterns = within(dialog).getByLabelText(/^console success patterns/i);
    await user.clear(patterns);
    await user.type(patterns, 'login:{enter}Reached target .*Multi-User');
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
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
    await user.click(await within(dialog).findByRole('checkbox', { name: 'Select web-01' }));
    await user.click(within(dialog).getByText(/^advanced:/i));
    const keepWarm = within(dialog).getByLabelText(/^keep-warm interval/i);
    expect(keepWarm).toHaveValue(15);
    await user.clear(keepWarm);
    await user.type(keepWarm, '30');
    const prestage = within(dialog).getByRole('group', { name: /pre-staged at the destination/i });
    expect(within(prestage).getAllByRole('checkbox')).toHaveLength(6);
    expect(within(prestage).getByRole('checkbox', { name: /^security group rules/i })).toBeChecked();
    await user.click(within(prestage).getByRole('checkbox', { name: /^security group rules/i }));
    await user.click(within(dialog).getByRole('button', { name: /create plan with 1 vm/i }));

    await waitFor(() => expect(server.plans.some((p) => p.name === 'Warm wave')).toBe(true));
    const created = server.plans.find((p) => p.name === 'Warm wave')!;
    expect(created.keep_warm_interval_s).toBe(1800);
    expect(created.prestage_resources).toEqual(['networks', 'subnets', 'routers', 'router_interfaces', 'security_groups']);
  });

  it('sets a per-VM strategy override; a VM taken out of the plan loses its override (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const { server } = renderWithApp(<Plans />, { route: '/plans', path: '/plans', token: 'operator' });

    await user.click(await screen.findByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.type(within(dialog).getByLabelText(/^name/i), 'Override wave');
    await user.selectOptions(within(dialog).getByLabelText(/source provider/i), 'rhosp17-dc1');
    await user.selectOptions(within(dialog).getByLabelText(/destination/i), 'rhoso-prod');
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
