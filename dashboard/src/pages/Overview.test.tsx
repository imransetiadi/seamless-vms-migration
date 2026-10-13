import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Migration } from '../api/types';
import { TONE_CLASSES } from '../lib/status';
import { createTestServer, renderWithApp } from '../test/utils';
import Overview from './Overview';

/** Every GET the page makes, as its path and query under /api/v1. */
function recordingReads(reads: string[]) {
  return (mock: typeof fetch): typeof fetch =>
    (input, init) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'GET') reads.push(String(input).replace(/^.*\/api\/v1/, ''));
      return mock(input, init);
    };
}

describe('Overview page (mock data)', () => {
  it('reads the migrations as summaries, never every migration whole (SDD §16, perf audit PF-02)', async () => {
    const reads: string[] = [];
    renderWithApp(<Overview />, { wrapFetch: recordingReads(reads) });
    await screen.findByRole('region', { name: 'Needs attention' });
    await waitFor(() => expect(reads.some((r) => r.startsWith('/migrations'))).toBe(true));
    await screen.findAllByRole('link', { name: /^[a-z0-9-]+$/ });
    expect(reads.filter((r) => r.startsWith('/migrations'))).toEqual(['/migrations?view=summary']);
  });

  it('leaves a cancelled plan out of the strictest SLO: it migrates nothing more (SDD §12)', async () => {
    const server = createTestServer();
    const cancelled = server.plans.find((p) => p.status === 'cancelled');
    if (!cancelled) throw new Error('fixture without a cancelled plan');
    cancelled.downtime_slo_s = 60;
    const others = server.plans.filter((p) => p.status !== 'draft' && p.status !== 'cancelled').map((p) => p.downtime_slo_s);
    expect(Math.min(...others)).toBeGreaterThan(60);
    renderWithApp(<Overview />, { route: '/', server, token: 'viewer' });
    const summaries = await screen.findAllByText(/the (Strictest )?SLO of /);
    for (const summary of summaries) expect(summary).not.toHaveTextContent('1m 00s');
  });

  it('counts plan progress over the migrations that are not cancelled, and a cancelled plan shows none (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.name === 'Pilot — static content')!;
    const ofPlan = server.migrations.filter((m) => m.plan_id === plan.id);
    for (const m of ofPlan) m.phase = 'finalized';
    ofPlan[0]!.phase = 'cancelled';
    renderWithApp(<Overview />, { route: '/', server, token: 'viewer' });
    const item = (await screen.findByRole('link', { name: plan.name })).closest('li')!;
    const n = ofPlan.length - 1;
    await waitFor(() => expect(item).toHaveTextContent(`${n} of ${n} done`));
    const cancelled = server.plans.find((p) => p.status === 'cancelled')!;
    const gone = screen.getByRole('link', { name: cancelled.name }).closest('li')!;
    expect(gone).not.toHaveTextContent(/of \d+ done/);
    expect(gone).toHaveTextContent('Nothing left to do');
  });

  it('marks a Completed plan where nothing was migrated, as every view does (SDD §16)', async () => {
    const server = createTestServer();
    const plan = server.plans.find((p) => p.name === 'Pilot — static content')!;
    for (const m of server.migrations.filter((x) => x.plan_id === plan.id)) m.phase = 'rolled_back';
    renderWithApp(<Overview />, { route: '/', server, token: 'viewer' });
    const item = (await screen.findByRole('link', { name: plan.name })).closest('li')!;
    expect(within(item).getByRole('button', { name: 'Completed' })).toHaveAccessibleDescription('Nothing was migrated: every migration rolled back.');
  });

  it('counts a plan before its first validation by its VMs, as the plans list does (SDD §16)', async () => {
    const server = createTestServer();
    const template = server.plans.find((p) => p.name === 'Pilot — static content')!;
    server.plans.push({ ...template, id: 'plan-0d0d0d0d', name: 'Not validated yet', vm_ids: ['vm-a', 'vm-b', 'vm-c'], waves: [], status: 'draft' });
    renderWithApp(<Overview />, { route: '/', server, token: 'viewer' });
    const item = (await screen.findByRole('link', { name: 'Not validated yet' })).closest('li')!;
    await waitFor(() => expect(item).toHaveTextContent('0 of 3 done'));
  });

  it('reads the summaries of the plan in its address alone (SDD §16, perf audit PF-02)', async () => {
    const reads: string[] = [];
    const server = createTestServer();
    const plan = server.plans[0]!;
    renderWithApp(<Overview />, { server, route: `/?plan=${plan.id}`, wrapFetch: recordingReads(reads) });
    await waitFor(() => expect(reads.some((r) => r.startsWith('/migrations'))).toBe(true));
    expect(reads.filter((r) => r.startsWith('/migrations'))).toEqual([`/migrations?plan_id=${plan.id}&view=summary`]);
  });

  it("keeps the figures readable while another plan's statistics load: greyed and busy, never faded (SDD §16)", async () => {
    const user = userEvent.setup({ delay: null });
    let hold = false;
    renderWithApp(<Overview />, {
      // once a plan is chosen, its statistics never answer: the previous figures stay on show
      wrapFetch: (mock) => (input, init) => (hold && String(input).split('?')[0]!.endsWith('/api/v1/stats') ? new Promise<Response>(() => {}) : mock(input, init)),
    });
    const strip = await screen.findByRole('region', { name: 'Key metrics' });
    expect(strip).not.toHaveAttribute('aria-busy');
    hold = true;
    await user.selectOptions(screen.getByLabelText('Plan'), 'plan-4f2a9c1e');
    await waitFor(() => expect(strip).toHaveAttribute('aria-busy', 'true'));
    expect(strip.className).not.toMatch(/opacity-/);
    const values = Array.from(strip.querySelectorAll('.num'));
    expect(values.length).toBeGreaterThan(0);
    for (const value of values) expect(value).toHaveClass('text-muted-foreground');
  });

  it('keeps a plan in the address that names no plan chosen, and says it does not exist instead of an all-clear (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Overview />, { route: '/?plan=plan-00000000', path: '/', token: 'viewer' });
    expect(await screen.findByText(/^plan plan-00000000 does not exist$/i)).toBeInTheDocument();
    const select = screen.getByLabelText(/^plan$/i);
    expect(select).toHaveValue('plan-00000000');
    // no zeros read as all clear
    expect(screen.queryByText(/no vm is down/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing needs attention/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /show all plans/i }));
    await waitFor(() => expect(screen.getByLabelText(/^plan$/i)).toHaveValue(''));
    expect(screen.queryByText(/does not exist/i)).not.toBeInTheDocument();
  });

  it('renders KPI tiles from /stats', async () => {
    const { server } = renderWithApp(<Overview />);

    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
    const total = await screen.findByRole('group', { name: 'Migrations' });
    expect(total).toHaveTextContent(String(server.migrations.length));
    const failed = server.migrations.filter((m) => m.phase === 'failed').length;
    expect(screen.getByRole('group', { name: 'Failed' })).toHaveTextContent(String(failed));
    expect(screen.getByRole('group', { name: 'SLO compliance' })).toHaveTextContent('%');
  });

  it('splits Completed into Awaiting finalize and Finalized (SDD §4.2, ADV-19)', async () => {
    const { server } = renderWithApp(<Overview />);
    const awaiting = await screen.findByRole('group', { name: 'Awaiting finalize' });
    expect(awaiting).toHaveTextContent(String(server.migrations.filter((m) => m.phase === 'completed').length));
    expect(screen.getByRole('group', { name: 'Finalized' })).toHaveTextContent(String(server.migrations.filter((m) => m.phase === 'finalized').length));
    expect(screen.queryByRole('group', { name: 'Completed' })).not.toBeInTheDocument();
  });

  it('says how many completed migrations the downtime figures leave out, and why (SDD §5.5, ADV-16)', async () => {
    const server = createTestServer();
    server.migrations.find((m) => m.id === 'mig-3c1a0f9e21')!.data_issues = ['DOWNTIME_BELOW_FINAL_SYNC: 6 s of downtime around a 258 s final sync'];
    renderWithApp(<Overview />, { server });
    const average = await screen.findByRole('group', { name: 'Average downtime' });
    expect(average).toHaveTextContent('1 left out: data inconsistent');
    expect(screen.getByRole('group', { name: 'SLO compliance' })).toHaveTextContent('1 left out: data inconsistent');
  });

  it('says nothing is left out while every completed migration holds together', async () => {
    renderWithApp(<Overview />);
    const average = await screen.findByRole('group', { name: 'Average downtime' });
    expect(average).not.toHaveTextContent(/left out/);
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

  it('runs the Downtime now clocks in model time in demo mode, as the migration page does (SDD §16, ADV-16)', async () => {
    const server = createTestServer();
    server.health = { ...server.health, demo_speed: 60 };
    const web03 = server.migrations.find((m) => m.vm.name === 'web-03')!;
    web03.downtime_started_at = new Date(Date.now() - 5_000).toISOString();
    renderWithApp(<Overview />, { server });
    const down = await screen.findByRole('region', { name: /downtime now/i });
    const item = (await within(down).findByRole('link', { name: 'web-03' })).closest('li')!;
    // about 5 s of wall time: about 5 minutes of model time
    await waitFor(() => expect(item).toHaveTextContent(/\b[5-6]:\d{2}of /));
    expect(down).toHaveTextContent('Demo time: 60× faster than real time.');
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
    expect(within(attention).getByRole('link', { name: 'gpu-render-01' })).toBeInTheDocument();
  });

  it('lists the pending approvals of both kinds once, each with its kind, filtered by kind (SDD §16, ADV-20)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Overview />);
    const inbox = await screen.findByRole('region', { name: 'Pending approvals' });
    const billing = await within(inbox).findByRole('listitem', { name: 'app-billing-01' });
    expect(billing).toHaveTextContent('Cutover approval');
    expect(billing).toHaveTextContent('Converged: waiting for an approval; keep-warm syncs run meanwhile.');
    expect(within(billing).getByRole('link', { name: 'app-billing-01' })).toHaveAttribute('href', expect.stringMatching(/^\/migrations\/mig-/));
    const held = within(inbox).getByRole('listitem', { name: 'analytics-node-2' });
    expect(held).toHaveTextContent('Advisor decision · Migration strategy');
    expect(held).toHaveTextContent('Shared Ceph handover — analytics');
    expect(within(held).getByRole('link', { name: 'analytics-node-2' })).toHaveAttribute('href', '/advisor/recommendations/rec-b2c4d6e87101');
    expect(within(inbox).getAllByRole('listitem')).toHaveLength(3);
    // listed once on the page: Needs attention leaves it to the inbox
    const attention = screen.getByRole('region', { name: 'Needs attention' });
    expect(within(attention).queryByRole('link', { name: 'app-billing-01' })).not.toBeInTheDocument();

    await user.click(within(inbox).getByRole('radio', { name: 'Advisor decision (1)' }));
    expect(within(inbox).getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))).toEqual(['analytics-node-2']);
    await user.click(within(inbox).getByRole('radio', { name: 'Cutover approval (2)' }));
    expect(within(inbox).getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))).toEqual(['ad-dc-01', 'app-billing-01']);
    await user.click(within(inbox).getByRole('radio', { name: 'All (3)' }));
    expect(within(inbox).getAllByRole('listitem')).toHaveLength(3);
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
    expect(screen.getAllByText(/unknown: the migrations could not be loaded/i)).toHaveLength(3);
    expect(screen.getAllByText(/unknown: the statistics could not be loaded/i)).toHaveLength(3);
    expect(screen.getByText(/unknown: the plans could not be loaded/i)).toBeInTheDocument();
    const metrics = screen.getByRole('region', { name: /key metrics/i });
    expect(within(metrics).getByRole('group', { name: 'Failed' })).toHaveTextContent('Failed—');
  });

  it('never reads a failed plans load as all clear while the migrations load (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/plans'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Overview />, { server });
    expect(await screen.findByText(/plans are unavailable/i)).toBeInTheDocument();
    // a converged migration still waits at the cutover gate: for what is unknown, never "nothing"
    const attention = await screen.findByRole('region', { name: 'Needs attention' });
    expect(await within(attention).findByRole('link', { name: 'app-billing-01' })).toBeInTheDocument();
    expect(within(attention).getByText(/waiting at the cutover gate/i)).toBeInTheDocument();
    expect(within(attention).getByText(/unknown: the plans could not be loaded/i)).toBeInTheDocument();
    expect(within(attention).queryByText(/nothing needs attention/i)).not.toBeInTheDocument();
    // the inbox cannot tell the cutover approvals, and still lists the advisor decisions (ADV-20)
    const inbox = screen.getByRole('region', { name: 'Pending approvals' });
    expect(within(inbox).getByText('Cutover approvals unknown: the plans could not be loaded.')).toBeInTheDocument();
    expect(within(inbox).getAllByRole('listitem').map((li) => li.getAttribute('aria-label'))).toEqual(['analytics-node-2']);
    // a down VM's SLO is unknown, not met
    const downtime = screen.getByRole('region', { name: 'Downtime now' });
    expect(within(downtime).getAllByText(/slo unknown: the plans could not be loaded/i).length).toBeGreaterThan(0);
    // no figure counts the plans or judges against an SLO it does not know
    const metrics = screen.getByRole('region', { name: /key metrics/i });
    const total = within(metrics).getByRole('group', { name: 'Migrations' });
    expect(total).not.toHaveTextContent(/in 0 plans/);
    expect(total).toHaveTextContent(/plans unknown/);
    const slo = screen.getByRole('region', { name: 'Downtime vs SLO' });
    expect(within(slo).getByText(/unknown: the plans could not be loaded/i)).toBeInTheDocument();
    // neither the legend nor the data table (rows rendered, closed) gives a verdict
    expect(within(slo).getAllByRole('row', { hidden: true }).length).toBeGreaterThan(1);
    expect(within(slo).queryByText(/within slo|over slo/i)).not.toBeInTheDocument();
  });

  it('says what needs attention is unknown, not nothing, when the plans fail and nothing else is waiting (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      if (method === 'GET' && path === '/plans') return { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } };
      const result = handle(method, path, query, body, token);
      // only finished migrations: nothing a human must do that the migrations alone could show
      return method === 'GET' && path === '/migrations' && Array.isArray(result.body)
        ? { ...result, body: (result.body as Migration[]).filter((m) => m.phase === 'completed') }
        : result;
    };
    renderWithApp(<Overview />, { server });
    const attention = await screen.findByRole('region', { name: 'Needs attention' });
    expect(await within(attention).findByText(/unknown: the plans could not be loaded/i)).toBeInTheDocument();
    expect(within(attention).queryByText(/nothing needs attention/i)).not.toBeInTheDocument();
  });

  it('waits for the plans before saying what needs attention (SDD §16)', async () => {
    const pending = new Promise<Response>(() => {});
    renderWithApp(<Overview />, {
      wrapFetch: (mock) => (input, init) => (String(input).split('?')[0]!.endsWith('/api/v1/plans') ? pending : mock(input, init)),
    });
    // the migrations have loaded (a down VM has its clock), the plans have not
    const downtime = await screen.findByRole('region', { name: 'Downtime now' });
    await within(downtime).findByRole('link', { name: 'web-02' });
    expect(screen.getByText('Loading items that need attention…')).toBeInTheDocument();
    expect(screen.queryByText(/nothing needs attention/i)).not.toBeInTheDocument();
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
