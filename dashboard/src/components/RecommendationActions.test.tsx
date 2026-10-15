import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { strategyRecommendation } from '../api/mockData';
import type { Migration, Strategy } from '../api/types';
import { AUTH_OFF_HINT, OWN_DECISION } from '../lib/roles';
import { summaryOf } from '../lib/summary';
import { createTestServer, renderWithApp } from '../test/utils';
import { RecommendationActions, type ActionMigration } from './RecommendationActions';

/** A pending decision recommending warm for a migration of the fixtures, eligible for `eligible` (SDD §14.6). */
function setup(eligible: Strategy[]) {
  const server = createTestServer();
  const base = server.migrations.find((m) => m.phase === 'ready')!;
  const estimates = base.estimates.map((e) => ({ ...e, eligible: eligible.includes(e.strategy) }));
  const migration: Migration = { ...base, estimates, strategy: 'cold' };
  const rec = strategyRecommendation({
    id: 'rec-c0000000beef',
    at: '2026-10-08T11:00:00Z',
    migration,
    providerId: 'rhosp17-dc1',
    option: 'warm',
    confidence: 0.9,
    options: ['cold', 'warm', 'storage_handover'],
    status: 'pending_approval',
    deterministic: 'cold',
    snapshotSha256: null,
  });
  return { server, migration, rec };
}

async function offered(migration: ActionMigration, rec: ReturnType<typeof setup>['rec'], server: ReturnType<typeof setup>['server']) {
  const user = userEvent.setup({ delay: null });
  const { unmount } = renderWithApp(<RecommendationActions rec={rec} migration={migration} />, { token: 'admin', server });
  const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
  const approve = within(group).getByRole('button', { name: 'Approve' });
  const override = within(group).getByRole('button', { name: 'Override' });
  const state = {
    approve: approve.getAttribute('aria-disabled') === 'true' ? 'disabled' : 'enabled',
    override: override.getAttribute('aria-disabled') === 'true' ? 'disabled' : 'enabled',
    alternatives: [] as string[],
  };
  if (state.override === 'enabled') {
    await user.click(override);
    const dialog = await screen.findByRole('alertdialog');
    state.alternatives = within(dialog)
      .getAllByRole('option')
      .map((o) => o.textContent ?? '');
  }
  unmount();
  return state;
}

describe('RecommendationActions (SDD §14.6)', () => {
  it('says, with authentication off, how to get past a decision only another person may make (SDD §13, §16)', async () => {
    const { server, migration, rec } = setup(['cold', 'warm']);
    // no token: the anonymous admin of a loopback demo, who asked for this decision
    renderWithApp(<RecommendationActions rec={{ ...rec, requested_by: 'anonymous' }} migration={migration} />, { token: null, server });
    const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
    const approve = within(group).getByRole('button', { name: 'Approve' });
    await waitFor(() => expect(approve).toHaveAttribute('aria-disabled', 'true'));
    expect(approve).toHaveAccessibleDescription(`${OWN_DECISION} ${AUTH_OFF_HINT}`);
    expect(within(group).getByRole('button', { name: 'Override' })).toHaveAccessibleDescription(`${OWN_DECISION} ${AUTH_OFF_HINT}`);
  });

  it("offers the same from a migration's summary as from the whole migration: the eligible strategies (SDD §4, perf audit PF-02)", async () => {
    const { server, migration, rec } = setup(['cold', 'warm']);
    const whole = await offered(migration, rec, server);
    const summary = await offered(summaryOf(migration), rec, server);
    expect(whole).toEqual({ approve: 'enabled', override: 'enabled', alternatives: ['Cold migration'] });
    expect(summary).toEqual(whole);
  });

  it('refuses both when the summary says nothing is eligible, as the whole migration does', async () => {
    const { server, migration, rec } = setup([]);
    const whole = await offered(migration, rec, server);
    expect(whole).toMatchObject({ approve: 'disabled', override: 'disabled' });
    expect(await offered(summaryOf(migration), rec, server)).toEqual(whole);
  });
});
