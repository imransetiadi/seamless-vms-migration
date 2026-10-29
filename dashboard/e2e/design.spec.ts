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
