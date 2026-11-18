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

for (const width of [390, 1440]) {
  test(`Overview jumps to a scoped queue and opens a migration from search at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/login');
    await page.getByLabel('API token', { exact: true }).fill('viewer');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.getByLabel('Plan', { exact: true }).selectOption('plan-4f2a9c1e');
    const queues = page.getByRole('navigation', { name: 'Migration queues' });
    const attention = queues.getByRole('link', { name: /Needs attention/ });
    await expect(attention).toBeInViewport();
    await attention.click();
    await expect(page.getByRole('region', { name: 'Needs attention' })).toBeFocused();
    await expect(page).toHaveURL(/\?plan=plan-4f2a9c1e#attention$/);

    const find = page.getByRole('button', { name: 'Find migration', exact: true });
    await find.click();
    const dialog = page.getByRole('dialog', { name: 'Find migration' });
    const search = dialog.getByRole('searchbox', { name: 'Search migrations' });
    await expect(search).toBeFocused();
    await dialog.evaluate((el) => Promise.all(el.getAnimations().map((animation) => animation.finished)).then(() => undefined));
    const inputTop = (await search.boundingBox())!.y;
    await search.fill('no-such-migration');
    await expect(dialog.getByText('No matching migrations')).toBeVisible();
    expect(Math.abs((await search.boundingBox())!.y - inputTop)).toBeLessThanOrEqual(1);
    await dialog.getByRole('button', { name: 'Clear search' }).click();
    await expect(search).toBeFocused();
    await search.fill('web-03');
    const result = dialog.getByRole('link', { name: /web-03/ });
    const destination = await result.getAttribute('href');
    expect(destination).toMatch(/^\/migrations\//);
    await result.click();
    await expect(page).toHaveURL(new RegExp(destination! + '$'));
    await expect(page.getByRole('heading', { level: 1 })).toContainText('web-03');
    await page.goBack();
    await expect(search).toHaveValue('web-03');
    await expect(search).toBeFocused();
    await expect(result).toHaveAttribute('href', destination!);
    await expect(page.getByLabel('Plan', { exact: true })).toHaveValue('plan-4f2a9c1e');
    await page.keyboard.press('Escape');
    await expect(find).toBeFocused();
    await find.click();
    await expect(search).toHaveValue('');
  });
}

test('a fresh Overview visit starts with its own search after a queue jump', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('viewer');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  const attention = page.getByRole('navigation', { name: 'Migration queues' }).getByRole('link', { name: /Needs attention/ });
  await attention.click();
  const find = page.getByRole('button', { name: 'Find migration', exact: true });
  await find.click();
  const dialog = page.getByRole('dialog', { name: 'Find migration' });
  await dialog.getByRole('searchbox').fill('web-03');
  await dialog.getByRole('link', { name: /web-03/ }).click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('web-03');
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Overview' }).click();
  await attention.click();
  await expect(page.getByRole('region', { name: 'Needs attention' })).toBeFocused();
  await expect(dialog).toHaveCount(0);
  await find.click();
  await expect(dialog.getByRole('searchbox')).toHaveValue('');
});

test('the migration finder keeps results reachable at 200% text on a short screen', async ({ page }) => {
  await page.setViewportSize({ width: 844, height: 320 });
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('viewer');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
  await page.getByRole('button', { name: 'Find migration', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Find migration' });
  await dialog.getByRole('searchbox').fill('web-03');
  const result = dialog.getByRole('link', { name: /web-03/ });
  await result.scrollIntoViewIfNeeded();
  await expect(result).toBeInViewport();
  await result.click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('web-03');
});

test('a failure count opens the matching VMs and Escape returns to the metric', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('viewer');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'View metrics' }).click();
  await expect(page.getByRole('heading', { name: 'Performance', exact: true })).toBeFocused();
  const failed = page.getByRole('button', { name: /^View failed migrations/ });
  await failed.click();
  const dialog = page.getByRole('dialog', { name: 'Find migration' });
  await expect(dialog.getByRole('link', { name: /legacy-rhel6-app.*Failed/i })).toBeVisible();
  await dialog.getByRole('button', { name: 'Remove Failed phase filter' }).click();
  await dialog.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText(/9–16/);
  const firstOnPage = dialog.getByRole('link').first();
  const destination = await firstOnPage.getAttribute('href');
  await firstOnPage.click();
  await expect(page).toHaveURL(new RegExp(destination! + '$'));
  await page.goBack();
  await expect(dialog.getByRole('status')).toContainText(/9–16/);
  await expect(firstOnPage).toHaveAttribute('href', destination!);
  await page.keyboard.press('Escape');
  await expect(failed).toBeFocused();
});
