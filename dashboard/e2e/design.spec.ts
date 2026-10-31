import { expect, test } from '@playwright/test';

test('login branding stays readable at 320px with 200% text', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).waitFor({ state: 'visible' });
  await page.evaluate(async () => {
    document.documentElement.style.fontSize = '200%';
    await document.fonts.ready;
  });
  const title = page.getByText('Seamless Migrate', { exact: true });
  expect(await title.evaluate((el) => el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))).toBeLessThanOrEqual(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
});

test('Overview keeps operational navigation clear on a phone', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const plans = page.getByRole('region', { name: 'Plans', exact: true });
  await plans.getByRole('link', { name: 'View all plans', exact: true }).click();
  await expect(page).toHaveURL(/\/plans$/);
  const create = page.getByRole('button', { name: 'New plan', exact: true });
  await expect(create).toBeVisible();
  expect((await create.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    const status = page.getByRole('table').getByText('Running', { exact: true }).filter({ visible: true });
    await expect(status).toBeVisible();
    expect(await status.evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      return range.getClientRects().length;
    })).toBe(1);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/providers');
  const check = page.getByRole('article').first().getByRole('button', { name: /^Check / });
  await expect(check).toBeVisible();
  expect((await check.boundingBox())!.height).toBeGreaterThanOrEqual(44);
});

test('the full sidebar shows every navigation item at 1280 × 900, and a shorter column scrolls as one (SDD §16)', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const column = page.getByTestId('app-navigation');
  const nav = column.getByRole('navigation', { name: 'Primary' });
  await expect(nav.getByRole('link', { name: 'Users', exact: true })).toBeAttached();
  // every item an admin's navigation holds is on the screen, none scrolled away in a box of its own
  const links = await nav.getByRole('link').all();
  expect(links.length).toBeGreaterThanOrEqual(11);
  for (const link of links) await expect(link).toBeInViewport({ ratio: 1 });
  expect(await nav.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(0);
  // the full sidebar's items are compact desktop targets (SDD §16, MASTER.md)
  expect((await nav.getByRole('link', { name: 'Overview', exact: true }).boundingBox())!.height).toBe(36);
  // and the session stays at the column's foot, sign out among it
  await expect(column.getByRole('button', { name: 'Sign out', exact: true })).toBeInViewport();
  // shorter than its items and session: the column scrolls as one, the items first, never the items alone
  await page.setViewportSize({ width: 1280, height: 640 });
  expect(await nav.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(0);
  expect(await column.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeGreaterThan(0);
  await column.getByRole('button', { name: 'Sign out', exact: true }).scrollIntoViewIfNeeded();
  await expect(column.getByRole('button', { name: 'Sign out', exact: true })).toBeInViewport();
  // the rail, from 768 px to 1279 px, keeps its items 44 px tall
  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(column).toHaveAttribute('data-nav', 'rail');
  expect((await nav.getByRole('link', { name: 'Overview', exact: true }).boundingBox())!.height).toBeGreaterThanOrEqual(44);
});
