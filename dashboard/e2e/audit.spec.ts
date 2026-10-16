import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page, path = '/') {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

test('a failed page chunk keeps navigation and offers an explicit reload', async ({ page }) => {
  await signIn(page);
  await page.route(/\/assets\/Plans-.*\.js$/, (route) => route.abort());
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Plans', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('This page could not be loaded');
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Overview', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Plans', exact: true }).click();
  await page.unroute(/\/assets\/Plans-.*\.js$/);
  await page.getByRole('button', { name: 'Reload page', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Plans', exact: true })).toBeVisible();
});

test('network, audit and input scrollers have named keyboard access', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, '/migrations/mig-5d7e2b4a12');
  await page.getByRole('button', { name: 'Edit network', exact: true }).focus();
  await page.keyboard.press('Tab');
  const network = page.getByRole('region', { name: 'NICs of app-billing-01', exact: true });
  await expect(network).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => network.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  await page.goto('/advisor/recommendations/rec-d4e6f8a09001');
  await page.getByRole('tab', { name: 'Audit', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Audit events of this recommendation' })).toHaveAttribute('tabindex', '0');
  await page.getByRole('tab', { name: 'Input', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Input snapshot JSON' })).toHaveAttribute('tabindex', '0');
});

test('a definition near the viewport bottom stays fully visible', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await signIn(page, '/advisor');
  // the demo's first tick finishes api-gw-02's validation and lists its recommendation at the top: wait for it, so the
  // list has stopped moving before the term is measured and hovered (it moved from under the pointer now and then)
  await expect(page.getByRole('link', { name: 'Details for api-gw-02' })).toBeVisible();
  const term = page.getByRole('button', { name: 'confidence', exact: true }).nth(3);
  await term.evaluate((el) => {
    el.scrollIntoView({ block: 'center' });
    window.scrollBy(0, el.getBoundingClientRect().bottom - innerHeight + 25);
  });
  const box = (await term.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  const tooltip = page.getByRole('tooltip').filter({ visible: true });
  await expect(tooltip).toBeVisible();
  await expect.poll(async () => (await tooltip.boundingBox())!.y + (await tooltip.boundingBox())!.height).toBeLessThanOrEqual(792);
  await page.keyboard.press('Escape');
});

test('mobile policy names and recommendation evidence keep readable widths', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await signIn(page, '/advisor/policies');
  await page.evaluate(() => document.fonts.ready);
  for (const heading of await page.locator('article h3').all()) expect((await heading.boundingBox())!.width).toBeGreaterThanOrEqual(240);
  await page.addStyleTag({ content: '* { letter-spacing: .12em !important; word-spacing: .16em !important; line-height: 1.5 !important; }' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await page.goto('/advisor/recommendations/rec-d4e6f8a09001');
  const value = page.getByRole('list', { name: 'Evidence items' }).getByRole('listitem').first().getByText(/^Estimated downtime/);
  await expect(value).toBeVisible();
  expect((await value.boundingBox())!.width).toBeGreaterThanOrEqual(200);
});

test('Plans keeps filters through detail, Back and reload, and clears them accessibly', async ({ page }) => {
  await signIn(page, '/plans');
  await page.getByLabel('Search plans').fill('Legacy');
  await page.getByRole('link', { name: 'Legacy apps', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Legacy apps', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel('Search plans')).toHaveValue('Legacy');
  await page.reload();
  await expect(page.getByLabel('Search plans')).toHaveValue('Legacy');
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(page.getByLabel('Search plans')).toHaveValue('');
  await expect(page.getByLabel('Search plans')).toBeFocused();
});

test('closing changed plan input asks before discarding it', async ({ page }) => {
  await signIn(page, '/plans');
  await page.getByRole('button', { name: 'New plan', exact: true }).click();
  await page.getByRole('dialog').getByLabel(/^Name/).fill('Keep this draft');
  await page.keyboard.press('Escape');
  const confirm = page.getByRole('alertdialog', { name: 'Discard unsaved plan changes?' });
  await expect(confirm).toBeVisible();
  await confirm.getByRole('button', { name: 'Keep editing' }).click();
  await expect(page.getByRole('dialog').getByLabel(/^Name/)).toHaveValue('Keep this draft');
  await expect(page.getByRole('dialog').getByLabel(/^Name/)).toBeFocused();
  await page.keyboard.press('Escape');
  await confirm.getByRole('button', { name: 'Discard changes' }).click();
  await expect(page.getByRole('alertdialog')).toBeHidden();
  await expect(page.getByRole('button', { name: 'New plan', exact: true })).toBeFocused();
  await page.getByRole('button', { name: 'New plan', exact: true }).click();
  await expect(page.getByRole('dialog').getByLabel(/^Name/)).toHaveValue('');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByRole('alertdialog')).toBeHidden();
});

test('Plans validates URL filters and keeps status and sorting after reload', async ({ page }) => {
  await signIn(page, '/plans?status=unknown&sort=unknown&direction=unknown');
  await expect(page.getByLabel('Status', { exact: true })).toHaveValue('all');
  await page.getByLabel('Status', { exact: true }).selectOption('validated');
  await page.getByRole('button', { name: 'Plan', exact: true }).click();
  await page.reload();
  await expect(page.getByLabel('Status', { exact: true })).toHaveValue('validated');
  await expect(page.getByRole('columnheader', { name: /^Plan/ })).toHaveAttribute('aria-sort', 'ascending');
  await expect(page.getByRole('table', { name: 'Migration plans' })).toContainText('Shared Ceph handover');
});

test('changing only VM selection in an existing plan also asks before discard', async ({ page }) => {
  await signIn(page, '/plans/plan-c81d44a0');
  await page.getByRole('button', { name: 'Edit plan', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit plan', exact: true });
  await dialog.getByRole('checkbox', { name: 'Select analytics-node-1', exact: true }).uncheck();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Keep editing' }).click();
  await expect(dialog.getByRole('checkbox', { name: 'Select analytics-node-1', exact: true })).not.toBeChecked();
});

test('compact navigation keeps identity, role and demo context visible', async ({ page }) => {
  await signIn(page);
  for (const width of [320, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByText('rina', { exact: true }).filter({ visible: true })).toHaveCount(1);
    await expect(page.getByText('Admin', { exact: true }).filter({ visible: true })).toHaveCount(1);
    await expect(page.getByText('Demo', { exact: true }).filter({ visible: true })).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  }
});

test('a disabled action near the bottom keeps its explanation inside the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await signIn(page, '/migrations/mig-5d7e2b4a12');
  const button = page.getByRole('button', { name: 'Edit probes', exact: true });
  await button.evaluate((el) => {
    el.scrollIntoView({ block: 'center' });
    window.scrollBy(0, el.getBoundingClientRect().bottom - innerHeight + 8);
  });
  const box = (await button.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  const note = page.getByRole('note').filter({ hasText: 'Only a draft or validated plan can be edited' });
  await expect(note).toBeVisible();
  expect((await note.boundingBox())!.y).toBeGreaterThanOrEqual(8);
  expect((await note.boundingBox())!.y + (await note.boundingBox())!.height).toBeLessThanOrEqual(792);
});
