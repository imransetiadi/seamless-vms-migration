import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { MockServer } from '../api/mock';
import { strategyRecommendation } from '../api/mockData';
import { createTestServer, renderWithApp } from '../test/utils';
import RecommendationDetail from './RecommendationDetail';

function renderRec(id: string, server?: MockServer) {
  return renderWithApp(<RecommendationDetail />, { route: `/advisor/recommendations/${id}`, path: '/advisor/recommendations/:recId', token: 'viewer', server });
}

describe('Recommendation detail (SDD §14.5, ADV-05)', () => {
  it('shows the recommendation, its decision and its audit trail', async () => {
    renderRec('rec-d4e6f8a09001');
    const user = userEvent.setup({ delay: null });
    expect(await screen.findByText('Decision: Cold migration — below threshold (57% < 60%), default kept')).toBeInTheDocument();
    expect(screen.getByText(/^Recommended: Warm migration/)).toHaveTextContent('Recommended: Warm migration · 57% confidence');
    expect(screen.getByRole('link', { name: 'static-cdn-01' })).toHaveAttribute('href', '/migrations/mig-d4e6f8a090');
    await user.click(screen.getByRole('tab', { name: 'Audit' }));
    const audit = screen.getByRole('tabpanel', { name: 'Audit' });
    const rows = within(await within(audit).findByRole('table')).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell').map((c) => c.textContent).slice(1))).toEqual([
      ['Recommendation created', 'system:advisor', 'jev-1.13.0', 'Warm migration recommended at 57%'],
      ['Decision: below threshold, default kept', 'policy:global-default', 'jev-1.13.0', 'Cold migration'],
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
    const user = userEvent.setup({ delay: null });
    renderRec(recs[0]!.id, server);
    await user.click(await screen.findByRole('tab', { name: 'Audit' }));
    const audit = screen.getByRole('tabpanel', { name: 'Audit' });
    const rows = within(await within(audit).findByRole('table')).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
  });

  it('says a legacy entry has neither audit events nor an input snapshot', async () => {
    const user = userEvent.setup({ delay: null });
    renderRec('rec-legacy-mig-b2c4d6e870-0');
    expect(await screen.findByText('Legacy')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Audit' }));
    expect(await within(screen.getByRole('tabpanel', { name: 'Audit' })).findByText(/written before the audit trail existed/i)).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Input' }));
    expect(within(screen.getByRole('tabpanel', { name: 'Input' })).getByText(/no input snapshot/i)).toBeInTheDocument();
  });

  it('opens on Details: the full distribution, labelled and highest first, and what confidence means (ADV-03)', async () => {
    renderRec('rec-d4e6f8a09001');
    const details = await screen.findByRole('tabpanel', { name: 'Details' });
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Details', 'Audit', 'Input']);
    const rows = within(within(details).getByRole('table')).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))).toEqual([
      ['Warm migration', '57%'],
      ['Cold migration', '43%'],
    ]);
    expect(details).toHaveTextContent("Confidence means Jev's calibrated confidence in its own recommendation, not the probability it gives that option.");
  });

  it('says when the recommendation does not exist', async () => {
    renderRec('rec-000000000000');
    expect(await screen.findByText(/recommendation could not be loaded/i)).toBeInTheDocument();
  });

  it('offers Approve and Override while the recommendation can still change its migration, to advisor.approve (SDD §14.6, ADV-04)', async () => {
    const server = createTestServer();
    const id = server.migrations.find((m) => m.id === 'mig-b2c4d6e871')!.advisor_hold!;
    const admin = renderWithApp(<RecommendationDetail />, { route: `/advisor/recommendations/${id}`, path: '/advisor/recommendations/:recId', token: 'admin', server });
    expect(await screen.findByRole('group', { name: 'Decide on the recommendation for analytics-node-2' })).toBeInTheDocument();
    admin.unmount();
    renderRec(id, server);
    await screen.findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    expect(screen.queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
  });

  it('offers no action on a recommendation whose migration has started', async () => {
    const { queryClient } = renderWithApp(<RecommendationDetail />, { route: '/advisor/recommendations/rec-d4e6f8a09001', path: '/advisor/recommendations/:recId', token: 'admin' });
    await screen.findByText('Decision: Cold migration — below threshold (57% < 60%), default kept');
    // absent once the migration and its newest recommendations are loaded, not merely before
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(queryClient.getQueryData(['migration', 'mig-d4e6f8a090'])).toMatchObject({ phase: 'finalized' });
    expect(screen.queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
  });

  it('offers no action on a recommendation a newer one replaced (SDD §14.6)', async () => {
    const server = createTestServer();
    const m = server.migrations.find((x) => x.id === 'mig-b2c4d6e871')!;
    const older = m.advisor_hold!;
    server.recommendations.push(
      strategyRecommendation({ id: 'rec-b2c4d6e87199', at: '2026-10-08T11:59:00Z', migration: m, providerId: 'rhosp17-dc1', option: 'warm', confidence: 0.9, options: ['cold', 'warm', 'storage_handover'], status: 'pending_approval', deterministic: 'storage_handover', snapshotSha256: null, policyId: 'pol-finance' }),
    );
    m.advisor_hold = 'rec-b2c4d6e87199';
    const { queryClient } = renderWithApp(<RecommendationDetail />, { route: `/advisor/recommendations/${older}`, path: '/advisor/recommendations/:recId', token: 'admin', server });
    await screen.findByText('Decision: Waiting for approval — policy pol-finance requires approval');
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(queryClient.getQueryData(['migration', 'mig-b2c4d6e871'])).toMatchObject({ phase: 'ready' });
    expect(screen.queryByRole('group', { name: /^Decide on the recommendation for/ })).not.toBeInTheDocument();
  });
});
