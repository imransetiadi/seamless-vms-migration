import { expect, test, type Page } from '@playwright/test';

// Mock mode accepts the role names as tokens (src/api/mock.ts).
async function signIn(page: Page, token = 'admin') {
  await page.goto('/login');
  await page.getByLabel(/token/i).fill(token);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

test('design tokens reach the browser through the Tailwind build', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (err) => errors.push(err.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  await signIn(page);
  // dark theme by default: --background is 15 23 42 (SDD §16)
  const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(background).toBe('rgb(15, 23, 42)');
  // utilities built from the CSS-first theme resolve (a card uses the card token)
  const card = page.locator('.card').first();
  await expect(card).toBeVisible();
  expect(await card.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(27, 35, 54)');
  // the sans font stack from the theme applies
  expect(await page.evaluate(() => getComputedStyle(document.body).fontFamily)).toMatch(/Fira Sans/);
  expect(errors).toEqual([]);
});

test('the main journey renders: overview, plans, plan detail, migration detail', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  await page.getByRole('link', { name: /^plans$/i }).first().click();
  await expect(page).toHaveURL(/\/plans$/);
  const planLink = page.locator('a[href^="/plans/plan-"]').first();
  await expect(planLink).toBeVisible();
  await planLink.click();
  await expect(page).toHaveURL(/\/plans\/plan-/);

  const migrationLink = page.locator('a[href^="/migrations/mig-"]').first();
  await expect(migrationLink).toBeVisible();
  await migrationLink.click();
  await expect(page).toHaveURL(/\/migrations\/mig-/);
  await expect(page.getByRole('heading', { name: /estimate inputs/i })).toBeVisible();
  await expect(page.getByRole('heading', { name: /^estimates$/i })).toBeVisible();
});

test('a viewer sees operator and approver actions soft-disabled', async ({ page }) => {
  await signIn(page, 'viewer');
  await page.goto('/plans');
  // the control is rendered (so the viewer learns it exists) but disabled with the reason
  const newPlan = page.getByRole('button', { name: /new plan/i }).first();
  await expect(newPlan).toBeVisible();
  await expect(newPlan).toHaveAttribute('aria-disabled', 'true');
  // an approver action on a completed migration: Finalize stays disabled for a viewer
  const planLink = page.locator('a[href^="/plans/plan-"]').first();
  await planLink.click();
  await page.locator('a[href^="/migrations/mig-"]').first().click();
  await expect(page).toHaveURL(/\/migrations\/mig-/);
  const actions = page.getByRole('group', { name: /migration actions/i });
  await expect(actions).toBeVisible();
  for (const button of await actions.getByRole('button').all()) {
    await expect(button).toHaveAttribute('aria-disabled', 'true');
  }
});

test('light theme switches the token values', async ({ page }) => {
  await signIn(page);
  // the sidebar toggle is labelled by its visible text (the header one by aria-label)
  await page.locator('aside').getByRole('button', { name: /^(light|dark) theme$/i }).click();
  await expect
    .poll(async () => page.evaluate(() => getComputedStyle(document.body).backgroundColor))
    .toBe('rgb(248, 250, 252)');
});

test('an admin connects a Kolla-Ansible source and tests the connection', async ({ page }) => {
  await signIn(page);
  await page.goto('/providers');
  await page.getByRole('button', { name: /^add provider$/i }).first().click();
  const dialog = page.getByRole('dialog', { name: /connect a cloud/i });
  await dialog.getByText('Kolla-Ansible', { exact: true }).click();
  await dialog.getByLabel(/^name/i).fill('Kolla Browser Test');
  await expect(dialog.getByLabel(/^id/i)).toHaveValue('kolla-browser-test');
  await dialog.getByLabel(/keystone url/i).fill('https://kolla-b.example.com:5000/v3');
  await dialog.getByLabel(/^user name/i).fill('svc-migrate');
  await dialog.getByLabel(/^password/i).fill('browser-secret');
  await dialog.getByLabel(/^project$/i).fill('admin');
  await dialog.getByRole('button', { name: /add and test connection/i }).click();
  const result = page.getByRole('dialog', { name: /kolla browser test is connected/i });
  await expect(result).toBeVisible();
  await result.getByRole('button', { name: /^done$/i }).click();
  const card = page.getByRole('article', { name: 'Kolla Browser Test' });
  await expect(card).toContainText('Kolla-Ansible');
  await expect(card).toContainText(/stored just now/i);
  await expect(page.locator('body')).not.toContainText('browser-secret');
});

test('no page scrolls sideways on a 320 px wide screen (NFR-08: WCAG 2.2 reflow)', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  // a viewer sees the most soft-disabled controls, each with a visually hidden reason
  await signIn(page, 'viewer');
  const pages: Array<[string, () => ReturnType<Page['locator']>]> = [
    ['/', () => page.getByRole('heading', { level: 1 })],
    ['/plans', () => page.locator('a[href^="/plans/plan-"]').first()],
    ['/plans/plan-4f2a9c1e', () => page.locator('a[href^="/migrations/mig-"]').first()],
    ['/migrations/mig-5d7e2b4a12', () => page.getByRole('button', { name: /^use /i }).first()],
    ['/providers', () => page.getByRole('heading', { name: 'Migrate from' })],
    ['/inventory', () => page.getByRole('heading', { name: 'Inventory', level: 1 })],
    ['/events', () => page.getByRole('heading', { level: 1 })],
    ['/advisor', () => page.getByRole('heading', { level: 1 })],
  ];
  for (const [path, ready] of pages) {
    await page.goto(path);
    await expect(ready()).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: /loading/i })).toHaveCount(0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `${path} is ${overflow} px wider than the screen`).toBe(0);
  }
});
