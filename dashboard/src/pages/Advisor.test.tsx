import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { strategyRecommendation } from '../api/mockData';
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
    const kept = await within(feed).findByText('Decision: Cold migration — below threshold, default kept');
    const entry = kept.closest('li')!;
    expect(entry).toHaveTextContent('Recommended: Warm migration · 57% confidence (threshold 60%)');
    expect(within(entry).getByRole('link', { name: 'static-cdn-01' })).toHaveAttribute('href', '/migrations/mig-d4e6f8a090');
    expect(within(feed).getAllByText('Decision: Warm migration — applied automatically by policy global-default').length).toBeGreaterThan(1);
    expect(within(feed).getByText('Decision: Warm migration — advisor unavailable, decided by rules')).toBeInTheDocument();
    // ADV-03: the word confidence explains itself
    expect(within(entry).getByText('confidence')).toHaveAccessibleDescription(/calibrated confidence in its own recommendation/);
    await screen.findByRole('region', { name: 'Recent advisor notes' });
    expect(document.body).not.toHaveTextContent(/selected/i);
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
    await within(feed).findByText('Decision: Cold migration — below threshold, default kept');
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
});
