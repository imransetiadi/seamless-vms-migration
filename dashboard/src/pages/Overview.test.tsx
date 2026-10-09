import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createTestServer, renderWithApp } from '../test/utils';
import Overview from './Overview';

describe('Overview page (mock data)', () => {
  it('renders KPI tiles from /stats', async () => {
    const { server } = renderWithApp(<Overview />);

    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    const total = await screen.findByRole('group', { name: 'Migrations' });
    expect(total).toHaveTextContent(String(server.migrations.length));
    const failed = server.migrations.filter((m) => m.phase === 'failed').length;
    expect(screen.getByRole('group', { name: 'Failed' })).toHaveTextContent(String(failed));
    expect(screen.getByRole('group', { name: 'SLO compliance' })).toHaveTextContent('%');
  });

  it('puts the VMs that are down right now first, with their clock and SLO budget', async () => {
    renderWithApp(<Overview />);

    const down = await screen.findByRole('region', { name: /downtime now/i });
    const link = await within(down).findByRole('link', { name: 'web-03' });
    expect(link).toHaveAttribute('href', '/migrations/mig-3c1a0f9e23');
    // what each one waits for: the final copy of the cutover, the boot check while verifying
    expect(link.closest('li')).toHaveTextContent(/final copy 62/i);
    expect(within(down).getByRole('link', { name: 'web-02' }).closest('li')).toHaveTextContent(/checking the boot on rhoso/i);
    const meter = within(down).getByRole('meter', { name: /web-03 downtime against the slo/i });
    expect(Number(meter.getAttribute('aria-valuemax'))).toBeGreaterThan(0);
    expect(down).toHaveTextContent(/left in the .* slo|over the slo by/i);
  });

  it('turns the clock red and says by how much once a cutover runs over its SLO', async () => {
    const server = createTestServer();
    const web03 = server.migrations.find((m) => m.vm.name === 'web-03');
    const slo = server.plans.find((p) => p.id === web03?.plan_id)?.downtime_slo_s;
    if (!web03 || !slo) throw new Error('fixture without web-03 and its plan SLO');
    web03.phase = 'cutover';
    web03.downtime_started_at = new Date(Date.now() - (slo + 125) * 1000).toISOString();
    renderWithApp(<Overview />, { server });

    const down = await screen.findByRole('region', { name: /downtime now/i });
    const meter = await within(down).findByRole('meter', { name: /web-03 downtime against the slo/i });
    expect(meter).toHaveAttribute('aria-valuenow', String(slo));
    expect(down).toHaveTextContent(/over the slo by 2m 0[5-9]s/i);
  });

  it('lists a failed cutover and a running rollback while their source is still stopped', async () => {
    renderWithApp(<Overview />);

    const down = await screen.findByRole('region', { name: /downtime now/i });
    // SDD §5.2: the clock runs from the source stop until the VM boots verified or the source runs again
    const failed = await within(down).findByRole('link', { name: 'legacy-rhel6-app' });
    expect(failed).toHaveAttribute('href', '/migrations/mig-e5f7a9b1a0');
    expect(failed.closest('li')).toHaveTextContent(/failed with the source stopped/i);
    const rollingBack = within(down).getByRole('link', { name: 'shared-disk-node-a' });
    expect(rollingBack.closest('li')).toHaveTextContent(/rolling back, restarting the source/i);
    // a finished rollback restarted its source: not down any more
    expect(within(down).queryByRole('link', { name: 'report-gen-01' })).not.toBeInTheDocument();
    // longest down first: the failed cutover (41 min) before the rollback (19 min) and the cutovers
    const order = within(down).getAllByRole('listitem').map((item) => within(item).getAllByRole('link')[0]?.textContent);
    expect(order).toEqual(['legacy-rhel6-app', 'shared-disk-node-a', 'web-02', 'web-03']);
    expect(down).toHaveTextContent(/or its source runs again/i);
  });

  it('says so when no VM is down, and how many wait for the cutover', async () => {
    const server = createTestServer();
    for (const m of server.migrations) {
      if (m.phase === 'cutover' || m.phase === 'verifying') {
        m.phase = 'awaiting_cutover';
        m.downtime_started_at = null;
      } else if (m.downtime_started_at && !m.downtime_ended_at) {
        // the failed cutover and the rollback of the fixture: their sources run again
        m.phase = 'rolled_back';
        m.downtime_ended_at = new Date().toISOString();
      }
    }
    const waiting = server.migrations.filter((m) => m.phase === 'awaiting_cutover').length;
    renderWithApp(<Overview />, { server });

    const down = await screen.findByRole('region', { name: /downtime now/i });
    expect(await within(down).findByText(/no vm is down right now/i)).toBeInTheDocument();
    expect(down).toHaveTextContent(`${waiting} migrations have converged and wait for the cutover`);
    expect(within(down).queryByRole('meter')).not.toBeInTheDocument();
  });

  it('lists what needs a human, with the reason', async () => {
    renderWithApp(<Overview />);

    const attention = await screen.findByRole('region', { name: /needs attention/i });
    await within(attention).findByRole('link', { name: 'legacy-rhel6-app' });
    expect(attention).toHaveTextContent(/source VM is stopped/i);
    expect(within(attention).getByRole('link', { name: 'app-billing-01' })).toBeInTheDocument();
    expect(attention).toHaveTextContent(/waiting for approval/i);
    expect(within(attention).getByRole('link', { name: 'gpu-render-01' })).toBeInTheDocument();
  });

  it('shows the phase distribution with labels and counts', async () => {
    renderWithApp(<Overview />);

    const distribution = await screen.findByRole('region', { name: /phase distribution/i });
    const cutover = await within(distribution).findByText('Awaiting cutover');
    expect(cutover.closest('li')).toHaveTextContent('1');
  });

  it('offers table views of the charts', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Overview />);

    const throughput = await screen.findByRole('region', { name: /throughput/i });
    // The summary is the visible caption (the SVG also carries it as <desc> for assistive tech).
    expect(await within(throughput).findByText(/peak/i, { selector: 'figcaption' })).toBeInTheDocument();
    await user.click(within(throughput).getByText(/show data table/i));
    expect(within(throughput).getAllByRole('row').length).toBeGreaterThan(10);

    const downtime = screen.getByRole('region', { name: /downtime vs slo/i });
    await user.click(within(downtime).getByText(/show data table/i));
    expect(within(downtime).getByRole('rowheader', { name: 'Warm' })).toBeInTheDocument();
  });

  it('scopes the dashboard to one plan', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Overview />);

    const select = await screen.findByLabelText('Plan');
    await screen.findByRole('option', { name: 'VMware exit — ERP' });
    await user.selectOptions(select, 'plan-7b3e0d52');
    const total = await screen.findByRole('group', { name: 'Migrations' });
    await within(total).findByText('5');
  });
});
