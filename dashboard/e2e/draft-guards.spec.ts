import { expect, test } from '@playwright/test';

test('changed provider, policy and plan drafts survive Back and rejected reload', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  for (const [path, heading, button] of [['/providers', 'Providers', 'Add provider'], ['/advisor/policies', 'Advisor policies', 'New policy'], ['/plans', 'Plans', 'New plan']]) {
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Overview', exact: true }).click();
    await page.getByRole('navigation', { name: 'Primary' }).locator(`a[href="${path}"]`).click();
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    await page.getByRole('button', { name: button, exact: true }).click();
    const name = page.getByRole('dialog').getByLabel(/^Name/);
    await name.fill('Keep this draft');
    await page.goBack();
    const warning = page.getByRole('alertdialog', { name: 'Discard unsaved changes?', exact: true });
    await expect(warning).toBeVisible();
    await warning.getByRole('button', { name: 'Keep editing' }).click();
    await expect(name).toHaveValue('Keep this draft');
    const unload = page.waitForEvent('dialog');
    // Chromium leaves the reload promise pending after beforeunload is cancelled.
    const reload = page.reload({ timeout: 1000 }).catch(() => null);
    const browserWarning = await unload;
    expect(browserWarning.type()).toBe('beforeunload');
    await browserWarning.dismiss();
    await reload;
    await expect(name).toHaveValue('Keep this draft');
    await page.goBack();
    await warning.getByRole('button', { name: 'Discard changes' }).click();
    await expect(page).not.toHaveURL(new RegExp(`${path}$`));
  }
});
