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

test('a viewer cannot reach approver actions', async ({ page }) => {
  await signIn(page, 'viewer');
  await page.goto('/plans');
  await expect(page.getByRole('button', { name: /new plan/i })).toHaveCount(0);
});

test('light theme switches the token values', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: /theme|light|dark/i }).first().click();
  await expect
    .poll(async () => page.evaluate(() => getComputedStyle(document.body).backgroundColor))
    .toBe('rgb(248, 250, 252)');
});
