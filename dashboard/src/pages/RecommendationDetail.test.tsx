import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { MockServer } from '../api/mock';
import { createTestServer, renderWithApp } from '../test/utils';
import RecommendationDetail from './RecommendationDetail';

function renderRec(id: string, server?: MockServer) {
  return renderWithApp(<RecommendationDetail />, { route: `/advisor/recommendations/${id}`, path: '/advisor/recommendations/:recId', token: 'viewer', server });
}

describe('Recommendation detail (SDD §14.5, ADV-05)', () => {
  it('shows the recommendation, its decision and its audit trail', async () => {
    renderRec('rec-d4e6f8a09001');
    expect(await screen.findByText('Decision: Cold — below threshold (57% < 60%)')).toBeInTheDocument();
    expect(screen.getByText('Recommended: Warm · 57% confidence')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'static-cdn-01' })).toHaveAttribute('href', '/migrations/mig-d4e6f8a090');
    const audit = screen.getByRole('tabpanel', { name: 'Audit' });
    const rows = within(await within(audit).findByRole('table')).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell').map((c) => c.textContent).slice(1))).toEqual([
      ['Recommendation created', 'system:advisor', 'jev-1.13.0', 'Warm recommended at 57%'],
      ['Decision: below threshold, default kept', 'policy:global-default', 'jev-1.13.0', 'Cold'],
    ]);
  });

  it('shows the redacted input and checks its SHA-256 in the browser', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const sha = server.recommendations.find((r) => r.id === 'rec-d4e6f8a09001')!.input_snapshot_sha256!;
    renderRec('rec-d4e6f8a09001', server);
    await user.click(await screen.findByRole('tab', { name: 'Input' }));
    const input = screen.getByRole('tabpanel', { name: 'Input' });
    expect(await within(input).findByText(/matches its SHA-256/i)).toBeInTheDocument();
    expect(input).toHaveTextContent(sha);
    expect(input).toHaveTextContent('Which migration strategy should be used for VM static-cdn-01');
  });

  it('says so when the input no longer matches its SHA-256', async () => {
    const user = userEvent.setup({ delay: null });
    const server = createTestServer();
    const sha = server.recommendations.find((r) => r.id === 'rec-d4e6f8a09001')!.input_snapshot_sha256!;
    server.snapshots.get(sha)!.payload.deterministic = 'warm'; // changed after it was stored
    renderRec('rec-d4e6f8a09001', server);
    await user.click(await screen.findByRole('tab', { name: 'Input' }));
    const input = screen.getByRole('tabpanel', { name: 'Input' });
    expect(await within(input).findByText(/does not match its SHA-256/i)).toBeInTheDocument();
  });

  it("lists only this recommendation's audit events when its migration has several", async () => {
    const server = createTestServer();
    // two validations: analytics-db ties cold and warm, so each records a recommendation
    for (let i = 0; i < 2; i += 1) server.handle('POST', '/plans/plan-c81d44a0/validate', new URLSearchParams(), undefined, 'operator');
    const recs = server.recommendations.filter((r) => r.subject.name === 'analytics-db');
    expect(recs).toHaveLength(2);
    renderRec(recs[0]!.id, server);
    const audit = await screen.findByRole('tabpanel', { name: 'Audit' });
    const rows = within(await within(audit).findByRole('table')).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
  });

  it('says a legacy entry has neither audit events nor an input snapshot', async () => {
    const user = userEvent.setup({ delay: null });
    renderRec('rec-legacy-mig-b2c4d6e870-0');
    expect(await screen.findByText('Legacy')).toBeInTheDocument();
    expect(await within(screen.getByRole('tabpanel', { name: 'Audit' })).findByText(/written before the audit trail existed/i)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Input' }));
    expect(within(screen.getByRole('tabpanel', { name: 'Input' })).getByText(/no input snapshot/i)).toBeInTheDocument();
  });

  it('says when the recommendation does not exist', async () => {
    renderRec('rec-000000000000');
    expect(await screen.findByText(/recommendation could not be loaded/i)).toBeInTheDocument();
  });
});
