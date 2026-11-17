import { expect, test, type Page } from '@playwright/test';

// These journeys include native unload warnings and several nested confirmations on both engines.
test.setTimeout(60_000);

async function signIn(page: Page) {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
}

for (const label of ['Hooks', 'Notifications']) {
  test(`${label} drafts survive section jumps, rejected navigation, Back and rejected reload`, async ({ page }) => {
    await signIn(page);
    const primary = page.getByRole('navigation', { name: 'Primary' });
    await primary.getByRole('link', { name: label, exact: true }).click();
    const name = page.getByRole('textbox', { name: 'Name', exact: true });
    await name.fill('keep-my-catalog-draft');
    await page.getByRole('navigation', { name: label === 'Hooks' ? 'Hook sections' : 'Notification sections' }).getByRole('link', { name: 'Catalog', exact: true }).click();
    await expect(page.getByRole('alertdialog')).toHaveCount(0);
    await expect(name).toHaveValue('keep-my-catalog-draft');
    // Back first returns from the section's fragment without abandoning the form.
    await page.goBack();
    await expect(page.getByRole('alertdialog')).toHaveCount(0);
    await primary.getByRole('link', { name: 'Overview', exact: true }).click();
    const warning = page.getByRole('alertdialog', { name: 'Discard unsaved changes?' });
    await expect(warning).toBeVisible();
    await warning.getByRole('button', { name: 'Keep editing', exact: true }).click();
    await expect(name).toHaveValue('keep-my-catalog-draft');
    const unload = page.waitForEvent('dialog');
    const reload = page.reload({ timeout: 1000 }).catch(() => null);
    const dialog = await unload;
    expect(dialog.type()).toBe('beforeunload');
    await dialog.dismiss();
    await reload;
    await expect(name).toHaveValue('keep-my-catalog-draft');
    await page.goBack();
    await expect(warning).toBeVisible();
    await warning.getByRole('button', { name: 'Discard changes', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  });
}

for (const width of [320, 1440]) {
  test(`an unsaved signing key survives Escape and clears only after discard at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await signIn(page);
    // Back within the app uses its route blocker; a document navigation uses the native unload warning.
    if (width < 768) {
      await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
      await page.getByRole('dialog', { name: 'Navigation', exact: true }).getByRole('link', { name: 'Notifications', exact: true }).click();
    } else await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Notifications', exact: true }).click();
    const trigger = page.getByRole('button', { name: 'Set secret for itsm-webhook', exact: true });
    await trigger.click();
    await page.getByRole('button', { name: 'Generate key', exact: true }).click();
    const key = page.getByRole('textbox', { name: 'Signing key', exact: true });
    const value = await key.inputValue();
    await page.keyboard.press('Escape');
    const warning = page.getByRole('alertdialog', { name: 'Discard unsaved changes?' });
    await warning.getByRole('button', { name: 'Keep editing', exact: true }).click();
    await expect(key).toHaveValue(value);
    await page.goBack();
    await expect(warning).toBeVisible();
    await warning.getByRole('button', { name: 'Keep editing', exact: true }).click();
    await expect(key).toHaveValue(value);
    const stored = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join(' '));
    expect(stored).not.toContain(value);
    await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    await warning.getByRole('button', { name: 'Discard changes', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await expect(key).toHaveValue('');
  });
}

for (const [path, noun, create, remove] of [
  ['/hooks', 'Hook', 'Register hook', 'Delete hook'],
  ['/notifications', 'Channel', 'Add channel', 'Delete channel'],
]) {
  test(`${noun} creation and deletion report the result and return focus to a surviving heading`, async ({ page }) => {
    await signIn(page);
    await page.goto(path!);
    await page.getByRole('textbox', { name: 'Name', exact: true }).fill('audit-temporary');
    if (noun === 'Hook') await page.getByRole('textbox', { name: 'Playbook path', exact: true }).fill('/opt/hooks/audit.yml');
    else await page.getByLabel('Kind', { exact: true }).selectOption('slack');
    await page.getByRole('button', { name: create!, exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: `${noun} audit-temporary ${noun === 'Hook' ? 'registered' : 'added'}.` })).toBeVisible();
    await page.getByRole('button', { name: 'Delete audit-temporary', exact: true }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: remove!, exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Catalog', exact: true })).toBeFocused();
    await expect(page.getByRole('status').filter({ hasText: `${noun} audit-temporary deleted.` })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete audit-temporary', exact: true })).toHaveCount(0);
  });
}

test('catalog shortcuts expose monitoring sections on a phone with enlarged text', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await signIn(page);
  for (const [path, navName] of [['/hooks', 'Hook sections'], ['/notifications', 'Notification sections']]) {
    await page.goto(path!);
    await page.addStyleTag({ content: 'html { font-size: 200%; }' });
    await page.evaluate(() => document.fonts.ready);
    const shortcuts = page.getByRole('navigation', { name: navName! });
    for (const link of await shortcuts.getByRole('link').all()) {
      const id = (await link.getAttribute('href'))!;
      await link.click();
      const section = page.locator(id);
      await expect(section).toBeFocused();
      const box = (await section.boundingBox())!;
      const nav = (await shortcuts.boundingBox())!;
      expect(box.y).toBeGreaterThanOrEqual(nav.y + nav.height);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    }
  }
});
