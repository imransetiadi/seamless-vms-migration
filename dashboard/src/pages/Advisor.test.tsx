import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { strategyRecommendation } from '../api/mockData';
import type { MockServer } from '../api/mock';
import type { RecommendationPage } from '../api/types';
import { createTestServer, renderWithApp } from '../test/utils';
import Advisor from './Advisor';

/** A mock server whose GET /migrations fails, as a control plane with a broken database would. */
function serverWithoutMigrations() {
  const server = createTestServer();
  const handle = server.handle.bind(server);
  server.handle = (method, path, query, body, token) =>
    method === 'GET' && path === '/migrations'
      ? { status: 503, body: { error: { code: 'unavailable', message: 'database unavailable' } } }
      : handle(method, path, query, body, token);
  return server;
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.search}</output>;
}

function total(server: MockServer, query: Record<string, string>): number {
  return (server.handle('GET', '/advisor/recommendations', new URLSearchParams(query), undefined, 'viewer').body as RecommendationPage).total;
}

function addRecommendations(server: MockServer, m: MockServer['migrations'][number], n: number) {
  for (let i = 0; i < n; i += 1) {
    server.recommendations.push(
      strategyRecommendation({
        id: `rec-b0000000${String(i).padStart(4, '0')}`,
        at: new Date(Date.parse('2026-09-01T00:00:00Z') + i * 60_000).toISOString(),
        migration: m,
        providerId: 'rhosp17-dc1',
        option: 'warm',
        confidence: 0.9,
        options: ['cold', 'warm'],
        status: 'auto_applied',
        deterministic: 'warm',
        snapshotSha256: null,
      }),
    );
  }
}

describe('Advisor page', () => {
  it('says the advisor notes could not be loaded instead of claiming there are none', async () => {
    renderWithApp(<Advisor />, { server: serverWithoutMigrations() });

    // a 5xx is retried twice with backoff before the query reports the error
    expect(await screen.findByText(/advisor notes are unavailable/i, {}, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.queryByText('No advisor notes yet.')).not.toBeInTheDocument();
  }, 15_000); // the 5xx retries take about 3 s

  it('says it is still checking agentmemory while the advisor status loads, not that it is off', async () => {
    const pending = new Promise<Response>(() => {});
    renderWithApp(<Advisor />, {
      token: 'operator',
      wrapFetch: (mock) => (input, init) => (String(input).split('?')[0]!.endsWith('/api/v1/advisor/status') ? pending : mock(input, init)),
    });
    const search = await screen.findByRole('button', { name: /^search$/i });
    expect(search).toHaveAccessibleDescription('Checking whether agentmemory is enabled…');
  });

  it('says the advisor status could not be loaded instead of that agentmemory is off (SDD §16)', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/advisor/status'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Advisor />, { token: 'operator', server });
    await screen.findByText(/advisor status is unavailable/i);
    expect(screen.getByRole('button', { name: /^search$/i })).toHaveAccessibleDescription('The advisor status could not be loaded.');
  });

  it('says agentmemory is not enabled only when the loaded status says so', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      const result = handle(method, path, query, body, token);
      if (method !== 'GET' || path !== '/advisor/status') return result;
      const status = result.body as { memory: Record<string, unknown> };
      return { ...result, body: { ...status, memory: { ...status.memory, enabled: false } } };
    };
    renderWithApp(<Advisor />, { token: 'operator', server });
    const search = await screen.findByRole('button', { name: /^search$/i });
    await waitFor(() => expect(search).toHaveAccessibleDescription('agentmemory is not enabled.'));
  });

  it('names what happened when agentmemory failed, as the API words it', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) => {
      const result = handle(method, path, query, body, token);
      if (method !== 'GET' || path !== '/advisor/status') return result;
      const status = result.body as { memory: Record<string, unknown> };
      return { ...result, body: { ...status, memory: { enabled: true, available: false, last_error: 'search timed out after 5 s' } } };
    };
    renderWithApp(<Advisor />, { token: 'operator', server });
    const card = await screen.findByRole('region', { name: 'agentmemory' });
    expect(within(card).getByText('Unavailable')).toBeInTheDocument();
    expect(within(card).getByText('Last error: search timed out after 5 s')).toBeInTheDocument();
  });

  it('keeps the search within the bounds the API accepts: 2000 characters, 1 to 20 results (SDD §12, §16)', async () => {
    renderWithApp(<Advisor />, { token: 'operator' });
    expect(await screen.findByLabelText('Describe the problem')).toHaveAttribute('maxlength', '2000');
    const counts = Array.from((screen.getByLabelText('Results') as HTMLSelectElement).options).map((o) => Number(o.value));
    expect(counts.length).toBeGreaterThan(0);
    expect(counts.every((n) => Number.isInteger(n) && n >= 1 && n <= 20)).toBe(true);
  });

  it('announces the error when the search is submitted empty (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Advisor />, { token: 'operator' });
    const search = await screen.findByRole('button', { name: /^search$/i });
    // the advisor status has loaded: agentmemory is enabled in the mock
    await waitFor(() => expect(search).not.toHaveAttribute('aria-disabled'));
    await user.click(search);
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a few words about the failure to search for.');
  });

  it('shows what the advisor recommended and what was decided, never which option was "selected" (SDD §16, ADV-01)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const kept = await within(feed).findByText('Decision: Cold migration — below threshold (57% < 60%), default kept');
    const entry = kept.closest('li')!;
    expect(entry).toHaveTextContent('Recommended: Warm migration · 57% confidence');
    expect(entry).not.toHaveTextContent('(threshold 60%)'); // the comparison carries it (the review of 10 October)
    expect(within(entry).getByRole('link', { name: 'static-cdn-01' })).toHaveAttribute('href', '/migrations/mig-d4e6f8a090');
    expect(within(feed).getAllByText(/^Decision: Warm migration — applied automatically by policy global-default \(\d+% ≥ 60%\)$/).length).toBeGreaterThan(1);
    expect(within(feed).getByText('Decision: Warm migration — advisor unavailable, decided by rules')).toBeInTheDocument();
    // ADV-03: the word confidence explains itself
    expect(within(entry).getByText('confidence')).toHaveAccessibleDescription(/calibrated confidence in its own recommendation/);
    await screen.findByRole('region', { name: 'Recent advisor notes' });
    expect(document.body).not.toHaveTextContent(/selected/i);
  });

  it('says on each entry what its evidence supports and opposes, or that it was too little (SDD §16, ADV-02)', async () => {
    const server = createTestServer();
    const rec = server.recommendations.find((r) => r.id === 'rec-d4e6f8a09001')!;
    const supports = rec.evidence.filter((i) => i.effect === 'supports').length;
    const opposes = rec.evidence.filter((i) => i.effect === 'opposes').length;
    // a decision that waited for more evidence, newest of all
    server.recommendations.push({
      ...rec,
      id: 'rec-limited00001',
      created_at: '2026-10-09T12:00:00Z',
      subject: { ...rec.subject, name: 'limited-01' },
      evidence: rec.evidence.filter((i) => i.effect === 'context'),
      decision: { ...rec.decision, status: 'pending_approval', reason_code: 'INSUFFICIENT_EVIDENCE', final_option: null },
    });
    renderWithApp(<Advisor />, { token: 'viewer', server });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const entry = (await within(feed).findByText('Decision: Cold migration — below threshold (57% < 60%), default kept')).closest('li')!;
    expect(entry).toHaveTextContent(`Evidence: ${supports} support${supports === 1 ? 's' : ''}, ${opposes} oppose${opposes === 1 ? 's' : ''}`);
    const limited = within(feed).getByRole('link', { name: 'Details for limited-01' }).closest('li')!;
    expect(limited).toHaveTextContent('Limited evidence: 0 items support or oppose');
  });

  it('lists a strategy note of an earlier version as a Legacy recommendation with its original text, not as a note (SDD §14.4)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const original = await within(feed).findByText(/^Original note: Storage handover recommended/);
    const entry = original.closest('li')!;
    expect(within(entry).getByText('Legacy')).toBeInTheDocument();
    expect(entry).toHaveTextContent('Decision: Storage handover — applied automatically');
    expect(entry).toHaveTextContent('Recommended: Storage handover · 71% confidence');
    const notes = await screen.findByRole('region', { name: 'Recent advisor notes' });
    await within(notes).findByText(/Classified as stateless_web/);
    expect(within(notes).queryByText(/Storage handover recommended/)).not.toBeInTheDocument();
  });

  it('pages through the recommendations twenty at a time', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    for (let i = 0; i < 25; i += 1) {
      server.recommendations.push(
        strategyRecommendation({
          id: `rec-a0000000${String(i).padStart(4, '0')}`,
          at: new Date(Date.parse('2026-09-01T00:00:00Z') + i * 60_000).toISOString(),
          migration: m,
          providerId: 'rhosp17-dc1',
          option: 'warm',
          confidence: 0.9,
          options: ['cold', 'warm'],
          status: 'auto_applied',
          deterministic: 'warm',
          snapshotSha256: null,
        }),
      );
    }
    renderWithApp(<Advisor />, { token: 'viewer', server });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const total = server.recommendations.length + 1; // and the legacy note
    expect(await within(feed).findByText(`1–20 of ${total}`)).toBeInTheDocument();
    expect(within(feed).getAllByText(/^Decision: /)).toHaveLength(20);
    expect(within(feed).getByRole('button', { name: /previous/i })).toHaveAttribute('aria-disabled', 'true');
    await user.click(within(feed).getByRole('button', { name: /next/i }));
    expect(await within(feed).findByText(`21–${total} of ${total}`)).toBeInTheDocument();
    expect(within(feed).getAllByText(/^Decision: /)).toHaveLength(total - 20);
    expect(within(feed).getByRole('button', { name: /next/i })).toHaveAttribute('aria-disabled', 'true');
  });

  it('filters the feed by plan and decision type, keeps both in the address and goes back to the first page (SDD §16)', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    addRecommendations(server, m, 25);
    renderWithApp(
      <>
        <Advisor />
        <Where />
      </>,
      { token: 'viewer', server, route: '/advisor' },
    );
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    await within(feed).findByText(/^1–20 of /);
    await user.click(within(feed).getByRole('button', { name: /next/i }));
    await within(feed).findByText(/^21–/);

    const plan = screen.getByLabelText('Plan');
    await user.selectOptions(plan, m.plan_id);
    const inPlan = total(server, { plan_id: m.plan_id });
    expect(await within(feed).findByText(`1–20 of ${inPlan}`)).toBeInTheDocument();
    expect(screen.getByTestId('where')).toHaveTextContent(`plan=${m.plan_id}`);
    expect(plan).toHaveFocus();

    await user.selectOptions(screen.getByLabelText('Decision'), 'workload_tier');
    const tiers = total(server, { plan_id: m.plan_id, decision_type: 'workload_tier' });
    expect(tiers).toBeLessThan(inPlan);
    await waitFor(() => expect(within(feed).queryAllByText(/^Decision: /)).toHaveLength(tiers));
    expect(screen.getByTestId('where')).toHaveTextContent('type=workload_tier');
  });

  it('opens with the filters the address holds', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.vm.name === 'analytics-db')!;
    addRecommendations(server, m, 25);
    renderWithApp(<Advisor />, { token: 'viewer', server, route: `/advisor?plan=${m.plan_id}&type=migration_strategy` });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const expected = total(server, { plan_id: m.plan_id, decision_type: 'migration_strategy' });
    expect(await within(feed).findByText(`1–20 of ${expected}`)).toBeInTheDocument();
    expect(screen.getByLabelText('Plan')).toHaveValue(m.plan_id);
    expect(screen.getByLabelText('Decision')).toHaveValue('migration_strategy');
  });

  it('says the recommendations could not be loaded instead of that there are none', async () => {
    const server = createTestServer();
    const handle = server.handle.bind(server);
    server.handle = (method, path, query, body, token) =>
      method === 'GET' && path === '/advisor/recommendations'
        ? { status: 400, body: { error: { code: 'bad_request', message: 'refused for the test' } } }
        : handle(method, path, query, body, token);
    renderWithApp(<Advisor />, { token: 'viewer', server });
    expect(await screen.findByText(/recommendations are unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText('No recommendations yet.')).not.toBeInTheDocument();
  });

  it('links to the audit trail and every recommendation to its detail (SDD §16, ADV-05)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    expect(await screen.findByRole('link', { name: /audit trail/i })).toHaveAttribute('href', '/advisor/audit');
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    await within(feed).findByText('Decision: Cold migration — below threshold (57% < 60%), default kept');
    expect(within(feed).getByRole('link', { name: 'Details for static-cdn-01' })).toHaveAttribute('href', '/advisor/recommendations/rec-d4e6f8a09001');
    expect(within(feed).getByRole('link', { name: 'Details for analytics-node-1' })).toHaveAttribute('href', '/advisor/recommendations/rec-legacy-mig-b2c4d6e870-0');
  });

  it('offers Approve and Override on the decision that waits, and focus stays on its entry once it is decided (SDD §14.6, §16, ADV-04)', async () => {
    const user = userEvent.setup({ delay: null });
    renderWithApp(<Advisor />, { token: 'admin' });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const waiting = await within(feed).findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    const entry = waiting.closest('li')!;
    // only the decision that waits: the others were taken
    expect(within(feed).getAllByRole('group', { name: /^Decide on the recommendation for/ })).toEqual([
      within(entry).getByRole('group', { name: 'Decide on the recommendation for analytics-node-2' }),
    ]);
    await user.click(within(entry).getByRole('button', { name: 'Approve' }));
    const confirm = await screen.findByRole('alertdialog', { name: 'Approve Storage handover for analytics-node-2?' });
    await user.click(within(confirm).getByRole('button', { name: 'Approve' }));
    expect(await within(feed).findByText('Decision: Storage handover — approved by rina')).toBeInTheDocument();
    expect(within(feed).queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
    expect(within(feed).getByRole('link', { name: 'Details for analytics-node-2' })).toHaveFocus();
    expect(screen.getByText('Approved: analytics-node-2 runs Storage handover.')).toHaveAttribute('role', 'status');
  });

  it('shows neither Approve nor Override without advisor.approve', async () => {
    renderWithApp(<Advisor />, { token: 'approver' });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    await within(feed).findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    expect(within(feed).queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
    expect(within(feed).queryByRole('button', { name: /approve|override/i })).not.toBeInTheDocument();
  });

  it('links to the advisor policies (SDD §16, ADV-04)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    expect(await screen.findByRole('link', { name: 'Policies' })).toHaveAttribute('href', '/advisor/policies');
  });

  it('says where the model runs, what each decision type sends it, and that the console stays local (SDD §13.4, ADV-06)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    const jev = await screen.findByRole('region', { name: 'Jev' });
    expect(await within(jev).findByText('Remote')).toBeInTheDocument();
    expect(jev).toHaveTextContent(/Console\s*Stays in this installation/);
    const panel = await screen.findByRole('region', { name: 'Data sent to advisor' });
    const strategy = await within(panel).findByRole('listitem', { name: 'Migration strategy' });
    expect(strategy).toHaveTextContent('jev_decide');
    expect(within(strategy).getByText('Guest write rate, measured or assumed')).toBeInTheDocument();
    const console = within(panel).getByRole('listitem', { name: 'Console screening' });
    expect(console).toHaveTextContent('Not sent');
    expect(console).toHaveTextContent(/stays in this installation/i);
  });

  it('shows where the model ran on each recommendation Jev answered, and nothing on the rules\' (ADV-06)', async () => {
    renderWithApp(<Advisor />, { token: 'viewer' });
    const feed = await screen.findByRole('region', { name: 'Recommendations' });
    const jevEntry = (await within(feed).findByText('Decision: Cold migration — below threshold (57% < 60%), default kept')).closest('li')!;
    expect(within(jevEntry).getByText('Remote')).toBeInTheDocument();
    const rulesEntry = within(feed).getByText('Decision: Warm migration — advisor unavailable, decided by rules').closest('li')!;
    expect(within(rulesEntry).queryByText(/^Remote|^On-prem/)).not.toBeInTheDocument();
  });
});
