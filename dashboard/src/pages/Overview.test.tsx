import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TONE_CLASSES } from '../lib/status';
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

  it.each([
    [0.78, 'calm'],
    [0.85, 'warning'],
  ])('reads a VM at %s of its SLO like the migration page does, the warning with an icon (SDD §16)', async (share, expected) => {
    const server = createTestServer();
    const web03 = server.migrations.find((m) => m.vm.name === 'web-03');
    const slo = server.plans.find((p) => p.id === web03?.plan_id)?.downtime_slo_s;
    if (!web03 || !slo) throw new Error('fixture without web-03 and its plan SLO');
    web03.phase = 'cutover';
    web03.downtime_started_at = new Date(Date.now() - share * slo * 1000).toISOString();
    renderWithApp(<Overview />, { server });
    const down = await screen.findByRole('region', { name: /downtime now/i });
    const item = (await within(down).findByRole('link', { name: 'web-03' })).closest('li')!;
    const line = within(item).getByText(/left in the .* slo/i);
    // the time left reads as a clock like the migration page's (SDD §16)
    expect(line).toHaveTextContent(/^\d+:\d{2} left in the /);
    if (expected === 'warning') {
      expect(line).toHaveClass(TONE_CLASSES.warning.text);
      expect(line.querySelector('.lucide-hourglass')).not.toBeNull();
    } else {
      expect(line).not.toHaveClass(TONE_CLASSES.warning.text);
      expect(line.querySelector('svg')).toBeNull();
    }
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
    expect(meter.getAttribute('aria-valuetext')).toMatch(/^\d+:\d{2} of \d+:\d{2}$/);
    // a clock like the migration page's, not a duration (SDD §16)
    expect(down).toHaveTextContent(/over the slo by 2:0[5-9]/i);
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

  it('never reads a failed load as all clear (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    // a 4xx is not retried: the error shows at once
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && (path === '/migrations' || path === '/plans' || path === '/stats')
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Overview />, { server });
    // each failed load is named, with Retry
    expect(await screen.findByText(/migrations are unavailable/i)).toBeInTheDocument();
    expect(await screen.findByText(/plans are unavailable/i)).toBeInTheDocument();
    expect(await screen.findByText(/statistics are unavailable/i)).toBeInTheDocument();
    // no panel built from them reads as all clear or keeps loading
    expect(screen.queryByText(/no vm is down right now/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing needs attention/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no plans yet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/loading/i)).not.toBeInTheDocument();
    // they say they are unknown instead, and the key figures are dashes, not zeros
    expect(screen.getAllByText(/unknown: the migrations could not be loaded/i)).toHaveLength(2);
    expect(screen.getAllByText(/unknown: the statistics could not be loaded/i)).toHaveLength(3);
    expect(screen.getByText(/unknown: the plans could not be loaded/i)).toBeInTheDocument();
    const metrics = screen.getByRole('region', { name: /key metrics/i });
    expect(within(metrics).getByRole('group', { name: 'Failed' })).toHaveTextContent('Failed—');
  });

  it('shows plan progress only once the migrations have loaded, never "0 of 0 done" before (SDD §16)', async () => {
    const pending = new Promise<Response>(() => {});
    const { server } = renderWithApp(<Overview />, {
      wrapFetch: (mock) => (input, init) => (String(input).split('?')[0]!.endsWith('/api/v1/migrations') ? pending : mock(input, init)),
    });
    // the plans have loaded (the plan picker lists them), the migrations have not
    await screen.findByRole('option', { name: server.plans[0]!.name });
    const plans = screen.getByRole('region', { name: 'Plans' });
    expect(within(plans).getByRole('status')).toBeInTheDocument();
    expect(within(plans).queryByText(/of \d+ done/)).not.toBeInTheDocument();
  });

  it('says plan progress is unknown when only the migrations cannot be loaded (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/migrations'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Overview />, { server });
    expect(await screen.findByText(/migrations are unavailable/i)).toBeInTheDocument();
    const plans = screen.getByRole('region', { name: 'Plans' });
    const name = server.plans[0]!.name;
    await within(plans).findByRole('link', { name });
    expect(within(plans).getAllByText(/progress unknown/i)).toHaveLength(server.plans.length);
    expect(within(plans).queryByText(/of \d+ done/)).not.toBeInTheDocument();
    expect(within(plans).queryByRole('progressbar')).not.toBeInTheDocument();
  });
});
