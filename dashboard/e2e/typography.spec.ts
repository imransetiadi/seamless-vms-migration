import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
}

test('navigation labels and activity dates stay at least 0.75rem', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  await signIn(page);
  await page.goto('/events');
  await expect(page.locator('main time').first()).toBeVisible();
  const small = await page.evaluate(() => {
    const minimum = parseFloat(getComputedStyle(document.documentElement).fontSize) * 0.75;
    return Array.from(document.querySelectorAll('aside a span, aside button span, main time span'))
      .filter((el) => el.getClientRects().length && !el.closest('.sr-only') && parseFloat(getComputedStyle(el).fontSize) < minimum)
      .map((el) => el.textContent);
  });
  expect(small).toEqual([]);
});

test('body copy and hints keep 1.5 line spacing', async ({ page }) => {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  const cramped = await page.locator('main p').evaluateAll((elements) => elements.filter((el) => {
    const css = getComputedStyle(el);
    return parseFloat(css.lineHeight) / parseFloat(css.fontSize) < 1.5;
  }).map((el) => el.textContent));
  expect(cramped).toEqual([]);
});

test('chart labels follow 200% root text size and stay keyboard-scrollable', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await signIn(page);
  for (const [path, name] of [['/', 'Throughput chart'], ['/migrations/mig-5d7e2b4a12', 'Sync passes chart']]) {
    await page.goto(path!);
    const chart = page.getByRole('region', { name: name!, exact: true });
    await expect(chart.locator('.recharts-text').first()).toBeVisible();
    await page.addStyleTag({ content: 'html { font-size: 200%; }' });
    await expect.poll(() => chart.locator('.recharts-text').first().evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBe(24);
    const sizes = await page.locator('.recharts-surface text').evaluateAll((elements) => elements.map((el) => parseFloat(getComputedStyle(el).fontSize)));
    expect(sizes.every((size) => size === 24)).toBe(true);
    await chart.focus();
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => chart.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  }
});

test('200% text keeps dashboard actions and status labels inside the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await signIn(page);
  for (const path of ['/', '/plans/plan-c81d44a0', '/migrations/mig-5d7e2b4a12', '/providers', '/inventory/rhosp17-dc1', '/inventory/rhoso-prod', '/events', '/advisor', '/advisor/recommendations/rec-b2c4d6e87101', '/advisor/policies']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: /loading/i })).toHaveCount(0);
    await page.addStyleTag({ content: 'html { font-size: 200%; }' });
    await page.evaluate(() => document.fonts.ready);
    await expect.poll(() => page.locator('main h1').evaluate((el) => parseFloat(getComputedStyle(el).fontSize)), { message: path }).toBe(40);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth), { message: path }).toBe(0);
  }
});
