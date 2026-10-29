import { expect, test as base } from '@playwright/test';
import { MockServer } from '../src/api/mock';

const test = base.extend<{ server: MockServer }>({
  server: async ({ page }, provide) => {
    const server = new MockServer();
    server.health.auth_disabled = false;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname.replace('/api/v1', '');
      if (path === '/events/stream') {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': heartbeat\n\n' });
        return;
      }
      const response = server.handle(request.method(), path, url.searchParams, request.postDataJSON(), request.headers().authorization?.replace('Bearer ', ''));
      await route.fulfill({ status: response.status, json: response.body ?? {} });
    });
    await page.goto('/login');
    await page.getByLabel('API token', { exact: true }).fill('admin');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page).not.toHaveURL(/login/);
    await provide(server);
  },
});

test('one-time token requires acknowledgement and warns before navigation', async ({ page, server }) => {
  expect(server.health.auth_disabled).toBe(false);
  await page.goto('/users');
  const name = page.getByLabel('User name');
  await expect(name).toBeVisible();
  await page.getByRole('button', { name: 'Add user', exact: true }).click();
  expect(await name.evaluate((input: HTMLInputElement) => input.validity.valueMissing)).toBe(true);
  await expect(name).toBeFocused();
  expect(server.handle('GET', '/users', new URLSearchParams(), undefined, 'admin').status).toBe(200);
  await name.fill('browser-audit-user');
  await page.getByRole('button', { name: 'Add user', exact: true }).click();
  const token = page.getByLabel('New user API token');
  await expect(token).toBeVisible();
  const createdToken = await token.inputValue();
  expect(createdToken.length).toBeGreaterThan(0);
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.reject(new Error('Clipboard unavailable')) } }));
  await page.getByRole('button', { name: 'Copy token', exact: true }).click();
  await expect(page.getByText('Copy failed. Select the token and copy it manually.')).toBeVisible();
  await token.focus();
  expect(await token.evaluate((input: HTMLTextAreaElement) => input.selectionEnd - input.selectionStart)).toBe(createdToken.length);
  const done = page.getByRole('button', { name: 'Done', exact: true });
  await expect(done).toHaveAttribute('aria-disabled', 'true');
  const overview = page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Overview', exact: true });
  await overview.click();
  await expect(page.getByRole('alertdialog')).toContainText('Leave without saving this token?');
  await page.getByRole('button', { name: 'Keep editing' }).click();
  await expect(token).toHaveValue(createdToken);
  const stored = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)]);
  expect(stored).not.toContain(createdToken);
  await page.getByLabel('I have saved this token securely').check();
  await done.click();
  await expect(token).toHaveCount(0);
  await expect(name).toBeFocused();
  await overview.click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await page.goBack();
  await expect(name).toBeVisible();
  await expect(token).toHaveCount(0);
  await expect(page.getByText('browser-audit-user', { exact: true })).toBeVisible();
});

test('older recommendation audit events remain reachable beyond the newest thousand', async ({ page, server }) => {
  const rec = server.recommendations.find((item) => item.id === 'rec-d4e6f8a09001')!;
  const expected = server.events.filter((event) => event.data.target_id === rec.id);
  expect(expected).toHaveLength(2);
  let seq = Math.max(...server.events.map((event) => event.seq));
  for (let index = 0; index < 1001; index++) {
    server.events.push({ seq: ++seq, ts: new Date().toISOString(), kind: 'recommendation.created', plan_id: rec.plan_id, migration_id: rec.migration_id, actor: 'system:advisor', message: 'Later recommendation', data: { target_id: `later-${index}` } });
  }
  await page.goto(`/advisor/recommendations/${rec.id}`);
  await page.getByRole('tab', { name: 'Audit', exact: true }).click();
  await expect(page.getByText(/older events may exist/)).toBeVisible();
  await page.getByRole('button', { name: 'Load older audit events', exact: true }).click();
  await expect(page.getByText('All available audit events loaded.')).toBeVisible();
  await expect(page.getByRole('table', { name: 'Audit events of this recommendation, oldest first' }).locator('tbody tr')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Load older audit events', exact: true })).toHaveCount(0);
});

test('investigation filters, sort and Pause survive navigation and Back', async ({ page, server }) => {
  expect(server.migrations.length).toBeGreaterThan(0);
  const plans = page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Plans', exact: true });
  await page.goto('/plans/plan-c81d44a0');
  const migrations = page.getByLabel('Search migrations', { exact: true });
  await migrations.fill('analytics-node-1');
  await page.getByRole('columnheader', { name: /^VM/ }).getByRole('button').click();
  const migrationSort = await page.getByRole('columnheader', { name: /^VM/ }).getAttribute('aria-sort');
  await page.getByRole('table', { name: /^Migrations in/ }).getByRole('link', { name: 'analytics-node-1', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'analytics-node-1', exact: true })).toBeVisible();
  await page.goBack();
  await expect(migrations).toHaveValue('analytics-node-1');
  await expect(page.getByRole('columnheader', { name: /^VM/ })).toHaveAttribute('aria-sort', migrationSort!);
  for (const [path, label, value] of [
    ['/inventory/rhosp17-dc1', 'Search VMs', 'web-01'],
    ['/events', 'Search events', 'web-01'],
    ['/advisor/audit', 'Search', 'web-01'],
  ]) {
    await page.goto(path!);
    const search = page.getByLabel(label!, { exact: true });
    await search.fill(value!);
    if (path === '/events') await page.getByRole('button', { name: 'Pause live updates', exact: true }).click();
    await plans.click();
    await expect(page.getByRole('heading', { name: 'Plans', exact: true })).toBeVisible();
    await page.goBack();
    await expect(search).toHaveValue(value!);
    if (path === '/events') await expect(page.getByRole('button', { name: 'Resume live updates', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await expect(search).toBeFocused();
    await expect(search).toHaveValue('');
  }
});

test('Advisor mobile shortcuts land and focus on incident search', async ({ page, server }) => {
  expect(server.recommendations.length).toBeGreaterThan(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/advisor');
  await page.getByRole('navigation', { name: 'Advisor sections' }).getByRole('link', { name: 'Similar incidents' }).click();
  const section = page.locator('#similar-incidents');
  await expect(section).toBeFocused();
  const box = await section.boundingBox();
  const shortcuts = await page.getByRole('navigation', { name: 'Advisor sections' }).boundingBox();
  expect(box!.y).toBeGreaterThanOrEqual(shortcuts!.y + shortcuts!.height);
  await expect(section.getByRole('searchbox', { name: 'Describe the problem' })).toBeInViewport();
});

for (const width of [320, 390, 768, 1440]) {
  test(`unbroken identities reflow at ${width}px in both themes`, async ({ page, server }) => {
    const name = 'v'.repeat(200);
    const failed = server.migrations.find((migration) => migration.phase === 'failed')!;
    const down = server.migrations.find((migration) => migration.phase === 'cutover')!;
    failed.vm.name = name;
    down.vm.name = name;
    server.providers[0]!.name = 'p'.repeat(200);
    const plan = server.plans.find((item) => item.id === failed.plan_id)!;
    plan.name = 'n'.repeat(200);
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['dark', 'light']) {
      await page.evaluate((value) => localStorage.setItem('seamless.theme', value), theme);
      for (const path of ['/', '/providers', `/migrations/${failed.id}`, `/plans/${plan.id}`]) {
        await page.goto(path);
        await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
        await expect(page.locator('html')).toHaveClass(new RegExp(theme));
        await expect(page.getByRole('status').filter({ hasText: /loading|reading/i })).toHaveCount(0);
        await page.evaluate(() => document.fonts.ready);
        const expected = path === '/providers' ? 'p'.repeat(200) : path.startsWith('/plans/') ? plan.name : name;
        await expect(page.getByText(expected, { exact: true }).first()).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), `${theme} ${path}`).toBe(0);
      }
    }
  });
}
