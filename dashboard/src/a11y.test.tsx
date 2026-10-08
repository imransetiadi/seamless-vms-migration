import { QueryClient } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import App from './App';
import { ApiClient } from './api/client';
import { createMockFetch } from './api/mock';
import { createTestServer } from './test/utils';

/*
 * Automated WCAG checks (axe-core) on every route of the real app shell, with mock data.
 * jsdom has no layout, so colour contrast is verified separately (theme/contrast.test.ts and the
 * headless-browser audit described in the report).
 */
const ROUTES: Array<[string, string]> = [
  ['/', 'Overview'],
  ['/plans', 'Plans'],
  ['/plans/plan-4f2a9c1e', 'DC1 → RHOSO production rollout'],
  ['/migrations/mig-5d7e2b4a12', 'app-billing-01'],
  ['/migrations/mig-e5f7a9b1a0', 'legacy-rhel6-app'],
  ['/providers', 'Providers'],
  ['/inventory/rhosp17-dc1', 'Inventory'],
  ['/inventory/rhoso-prod', 'Inventory'],
  ['/events', 'Events'],
  ['/advisor', 'Advisor'],
  ['/login', 'Sign in'],
];

async function violations(): Promise<Array<{ id: string; impact: string | null | undefined; help: string; targets: string[] }>> {
  const results = await axe.run(document.body, {
    rules: { 'color-contrast': { enabled: false } },
    resultTypes: ['violations'],
  });
  return results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    targets: v.nodes.slice(0, 4).map((n) => n.target.join(' ')),
  }));
}

function renderAt(path: string, token = 'admin') {
  window.history.pushState({}, '', path);
  const server = createTestServer();
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<App client={client} queryClient={queryClient} />);
}

async function settled() {
  await waitFor(() => expect(screen.queryAllByText(/^Loading|Checking your session/i)).toHaveLength(0), { timeout: 8_000 });
}

beforeAll(async () => {
  await Promise.all(
    ['Overview', 'Plans', 'PlanDetail', 'MigrationDetail', 'Providers', 'Inventory', 'Events', 'Advisor', 'Login'].map(
      (page) => import(`./pages/${page}.tsx`),
    ),
  );
}, 60_000);

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('axe-core: no WCAG violations', () => {
  it.each(ROUTES)('%s', async (path, heading) => {
    renderAt(path);
    expect(await screen.findByRole('heading', { level: 1, name: heading }, { timeout: 8_000 })).toBeInTheDocument();
    await settled();
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the finalize confirmation dialog', async () => {
    const user = userEvent.setup();
    renderAt('/migrations/mig-3c1a0f9e21', 'approver');
    await screen.findByRole('heading', { level: 1, name: 'web-01' }, { timeout: 8_000 });
    await settled();
    const group = screen.getByRole('group', { name: /migration actions/i });
    await user.click(within(group).getByRole('button', { name: /finalize/i }));
    await screen.findByRole('alertdialog', { name: /finalize web-01/i });
    expect(await violations()).toEqual([]);
  }, 30_000);

  it('the new-plan dialog with validation errors', async () => {
    const user = userEvent.setup();
    renderAt('/plans', 'operator');
    await screen.findByRole('heading', { level: 1, name: 'Plans' }, { timeout: 8_000 });
    await settled();
    await user.click(screen.getByRole('button', { name: /new plan/i }));
    const dialog = await screen.findByRole('dialog', { name: /new migration plan/i });
    await user.click(within(dialog).getByRole('button', { name: /^create plan/i }));
    await within(dialog).findByRole('alert');
    expect(await violations()).toEqual([]);
  }, 30_000);
});
