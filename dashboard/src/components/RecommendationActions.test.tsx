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

/** The fixtures' plan pins this VM (its seeded override): without it, only what a test sets decides. */
function unpinned(eligible: Strategy[]) {
  const made = setup(eligible);
  const plan = made.server.plans.find((p) => p.id === made.migration.plan_id)!;
  plan.strategy_overrides = Object.fromEntries(Object.entries(plan.strategy_overrides).filter(([vm]) => vm !== made.migration.vm.source_id));
  return { ...made, plan };
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

  it("refuses to approve an answer that could not be applied, naming why, and still offers an override (SDD §14.6, audit AD-03)", async () => {
    const { server, migration, rec } = unpinned(['cold', 'warm']);
    const why = "the advisor's answer was escalate";
    const unusable = { ...rec, recommendation: { ...rec.recommendation, unusable: why } };
    renderWithApp(<RecommendationActions rec={unusable} migration={migration} />, { token: 'admin', server });
    const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
    const approve = within(group).getByRole('button', { name: 'Approve' });
    await waitFor(() => expect(approve).toHaveAccessibleDescription(`nothing to approve: ${why}`));
    expect(approve).toHaveAttribute('aria-disabled', 'true');
    expect(within(group).getByRole('button', { name: 'Override' })).not.toHaveAttribute('aria-disabled');
  });

  it("offers the option of an answer that could not be applied in the override: Approve cannot run it (SDD §14.6, review RV50-05)", async () => {
    const { server, migration, rec } = unpinned(['cold', 'warm']);
    const unusable = { ...rec, recommendation: { ...rec.recommendation, unusable: "the advisor's answer was escalate" } };
    expect(await offered(migration, unusable, server)).toEqual({ approve: 'disabled', override: 'enabled', alternatives: ['Cold migration', 'Warm migration'] });
  });

  it("refuses both while the plan's default strategy sets the VM's, as the API does (SDD §14.6, audit AD-04)", async () => {
    const { server, migration, rec, plan } = unpinned(['cold', 'warm']);
    plan.default_strategy = 'cold';
    renderWithApp(<RecommendationActions rec={rec} migration={migration} />, { token: 'admin', server });
    const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
    const reason = `The plan sets the strategy of ${rec.subject.name} (Cold migration): change it in the plan.`;
    for (const name of ['Approve', 'Override']) {
      const button = within(group).getByRole('button', { name });
      await waitFor(() => expect(button).toHaveAccessibleDescription(reason));
      expect(button).toHaveAttribute('aria-disabled', 'true');
    }
  });

  it('counts the override reason as the API does: five emoji are five characters, not ten (SDD §14.6, audit AD-10)', async () => {
    const { server, migration } = unpinned(['cold', 'warm']);
    const rec = strategyRecommendation({
      id: 'rec-c0000000beef',
      at: '2026-10-08T11:00:00Z',
      migration,
      providerId: 'rhosp17-dc1',
      option: 'warm',
      confidence: 0.9,
      options: ['cold', 'warm'],
      status: 'pending_approval',
      deterministic: 'cold',
      snapshotSha256: null,
      policyId: 'pol-finance',
      rules: { require_distinct_approver: true, require_override_reason: true },
    });
    const user = userEvent.setup({ delay: null });
    renderWithApp(<RecommendationActions rec={rec} migration={migration} />, { token: 'admin', server });
    const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    const dialog = await screen.findByRole('alertdialog');
    const field = within(dialog).getByRole('textbox', { name: /Reason/ });
    await user.type(field, '\u{1F600}'.repeat(5));
    expect(within(dialog).getByRole('button', { name: 'Override' })).toHaveAttribute('aria-disabled', 'true');
    await user.type(field, '\u{1F600}'.repeat(5));
    expect(within(dialog).getByRole('button', { name: 'Override' })).not.toHaveAttribute('aria-disabled');
  });

  it('sends the override reason it counted, and holds one of 2000 characters as the API counts them (SDD §14.6, review RV50-06)', async () => {
    const { server, migration } = unpinned(['cold', 'warm']);
    const rec = strategyRecommendation({
      id: 'rec-c0000000beef',
      at: '2026-10-08T11:00:00Z',
      migration,
      providerId: 'rhosp17-dc1',
      option: 'warm',
      confidence: 0.9,
      options: ['cold', 'warm'],
      status: 'pending_approval',
      deterministic: 'cold',
      snapshotSha256: null,
      policyId: 'pol-finance',
      rules: { require_distinct_approver: true, require_override_reason: true },
    });
    server.recommendations.push(rec);
    const user = userEvent.setup({ delay: null });
    renderWithApp(<RecommendationActions rec={rec} migration={migration} />, { token: 'admin', server });
    const group = await screen.findByRole('group', { name: /^Decide on the recommendation for/ });
    await user.click(within(group).getByRole('button', { name: 'Override' }));
    const dialog = await screen.findByRole('alertdialog');
    const field = within(dialog).getByRole('textbox', { name: /Reason/ }) as HTMLTextAreaElement;
    const confirm = within(dialog).getByRole('button', { name: 'Override' });
    // 1500 emoji are 1500 characters to the API, 3000 units to the browser
    await user.click(field);
    await user.paste('\u{1F600}'.repeat(1500));
    expect([...field.value].length).toBe(1500);
    await user.clear(field);
    await user.paste('a'.repeat(2001));
    expect(confirm).toHaveAttribute('aria-disabled', 'true');
    expect(confirm).toHaveAccessibleDescription('At most 2000 characters.');
    // a byte-order mark Python's strip keeps: ten characters counted, ten sent
    await user.clear(field);
    await user.type(field, '\uFEFFabcdefghi');
    expect(confirm).not.toHaveAttribute('aria-disabled');
    await user.click(confirm);
    await waitFor(() => expect(server.recommendations.find((r) => r.id === rec.id)!.decision).toMatchObject({ status: 'overridden', reason_text: '\uFEFFabcdefghi' }));
  });
});
