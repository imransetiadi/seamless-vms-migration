import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { buildFixtures } from '../api/mockData';
import { createTestServer, renderWithApp, TEST_NOW } from '../test/utils';
import MigrationDetail from './MigrationDetail';

const fx = buildFixtures(TEST_NOW);

function inPlan(planId: string) {
  const m = fx.migrations.find((x) => x.plan_id === planId);
  if (!m) throw new Error(`no migration in ${planId}`);
  return m;
}

function renderCounting(migrationId: string) {
  const listed: string[] = [];
  renderWithApp(<MigrationDetail />, {
    route: `/migrations/${migrationId}`,
    path: '/migrations/:migrationId',
    token: 'operator',
    server: createTestServer(),
    wrapFetch: (mock) => (input, init) => {
      const url = new URL(String(input), 'http://x');
      if (url.pathname.endsWith('/api/v1/migrations')) listed.push(url.search);
      return mock(input, init);
    },
  });
  return listed;
}

describe('MigrationDetail requests', () => {
  it('does not load the whole plan’s migrations while the plan runs: only a draft or validated plan is editable', async () => {
    const m = inPlan('plan-4f2a9c1e'); // running
    const listed = renderCounting(m.id);
    const edit = await screen.findByRole('button', { name: 'Edit probes' });
    await waitFor(() => expect(edit).toHaveAccessibleDescription(/only a draft or validated plan can be edited \(this one is running\)/i));
    expect(listed).toEqual([]);
  });

  it('still loads them for a validated plan, whose edit waits for migrations in flight (SDD §12)', async () => {
    const m = inPlan('plan-c81d44a0'); // validated
    const listed = renderCounting(m.id);
    await screen.findByRole('button', { name: 'Edit probes' });
    await waitFor(() => expect(listed).toContainEqual(expect.stringContaining(`plan_id=${m.plan_id}`)));
  });
});
