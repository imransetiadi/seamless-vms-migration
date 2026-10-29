import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page, path = '/') {
  await page.goto('/login');
  await page.getByLabel('API token', { exact: true }).fill('admin');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

test('sign-in keeps the pending token stable and becomes editable after rejection', async ({ page }) => {
  await page.goto('/login');
  const token = page.getByLabel('API token', { exact: true });
  await token.fill('not-a-valid-token');
  // the page's clock runs from install on, so a pause at the runner's "now" could already lie in
  // its past on a busy runner ("Cannot fast-forward to the past"): pause 5 s after a known start -
  // short of every timer the sign-in page holds (the health query's 60 s)
  const start = new Date('2026-01-05T09:00:00Z');
  await page.clock.install({ time: start });
  await page.clock.pauseAt(new Date(start.getTime() + 5_000));
  await token.press('Enter');
  await expect(token).toHaveAttribute('readonly');
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toHaveAttribute('aria-busy', 'true');
  await token.press('Enter');
  await page.clock.runFor(500);
  await expect(page.getByRole('alert')).toContainText('The token was rejected.');
  await expect(token).not.toHaveAttribute('readonly');
  await expect(token).toBeFocused();
  await expect(token).toHaveValue('not-a-valid-token');
  await token.fill('admin');
  await token.press('Enter');
  await page.clock.runFor(500);
  await expect(page).toHaveURL(/\/$/);
});

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
  await page.setViewportSize({ width: 320, height: 844 });
  await signIn(page, '/migrations/mig-5d7e2b4a12');
  await page.getByRole('button', { name: 'Edit network', exact: true }).focus();
  await page.keyboard.press('Tab');
  const network = page.getByRole('region', { name: 'NICs of app-billing-01', exact: true });
  await expect(network).toBeFocused();
  expect(await network.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeGreaterThan(0);
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
  const tooltip = page.getByRole('tooltip').filter({ visible: true });
  // a later tick can still move the list from under the pointer (twice in one run of PR #49's CI): place the term at
  // the bottom, enter it afresh and look again until its definition opens - it opens on entering, at once
  await expect(async () => {
    await term.evaluate((el) => {
      el.scrollIntoView({ block: 'center' });
      window.scrollBy(0, el.getBoundingClientRect().bottom - innerHeight + 25);
    });
    const box = (await term.boundingBox())!;
    await page.mouse.move(0, 0);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await expect(tooltip).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
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

for (const width of [320, 1440]) {
  test(`an error summary keeps reverse Tab inside the plan dialog at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await signIn(page, '/plans');
    await page.getByRole('button', { name: 'New plan', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'New migration plan', exact: true });
    const submit = dialog.getByRole('button', { name: /^Create plan/ });
    await submit.click();
    await expect(dialog.getByRole('alert')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(submit).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(dialog.getByRole('link', { name: 'Enter a plan name.', exact: true })).toBeFocused();
    await expect(page.locator('#root')).toHaveAttribute('inert', '');
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(page.locator('#root')).not.toHaveAttribute('inert');
    await expect(page.getByRole('button', { name: 'New plan', exact: true })).toBeFocused();
  });
}

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

for (const width of [320, 1440]) {
  test(`activity filters recover empty results without losing focus at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await signIn(page);
    for (const theme of ['light', 'dark']) {
      await page.goto('/events');
      await page.evaluate((value) => { document.documentElement.className = value; }, theme);
      await page.getByRole('button', { name: 'Pause live updates', exact: true }).click();
      await page.getByLabel('Category', { exact: true }).selectOption('security');
      await page.getByLabel('Plan', { exact: true }).selectOption('plan-c81d44a0');
      await page.getByLabel('Show progress updates', { exact: true }).check();
      const search = page.getByLabel('Search events', { exact: true });
      await search.fill('no-matching-event');
      await expect(page.getByText('No events match', { exact: true })).toBeVisible();
      const clear = page.getByRole('button', { name: 'Clear filters', exact: true });
      expect((await clear.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      await clear.click();
      await expect(search).toBeFocused();
      await expect(search).toHaveValue('');
      await expect(page.getByLabel('Category', { exact: true })).toHaveValue('all');
      await expect(page.getByLabel('Plan', { exact: true })).toHaveValue('');
      await expect(page.getByLabel('Show progress updates', { exact: true })).not.toBeChecked();
      await expect(page).not.toHaveURL(/plan=/);
      await expect(page.getByRole('button', { name: 'Resume live updates', exact: true })).toBeVisible();
      await expect(page.getByRole('list', { name: 'Events, newest first', exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);

      await page.goto('/advisor/audit');
      await page.getByRole('table', { name: 'Advisor audit events, newest first', exact: true }).waitFor();
      await page.evaluate((value) => { document.documentElement.className = value; }, theme);
      await page.getByLabel('Action', { exact: true }).selectOption('decision.fallback');
      await page.getByLabel('Actor', { exact: true }).selectOption('system:advisor');
      const auditSearch = page.getByLabel('Search', { exact: true });
      await auditSearch.fill('no-matching-event');
      await expect(page.getByText('No audit events match', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
      await expect(auditSearch).toBeFocused();
      await expect(auditSearch).toHaveValue('');
      await expect(page.getByLabel('Action', { exact: true })).toHaveValue('all');
      await expect(page.getByLabel('Actor', { exact: true })).toHaveValue('all');
      await expect(page.getByRole('table', { name: 'Advisor audit events, newest first', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Clear filters', exact: true })).toBeHidden();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    }
  });
}

for (const width of [320, 768, 1440]) {
  test(`detail section jumps keep focus and headings clear at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await signIn(page, '/migrations/mig-5d7e2b4a12');
    for (const [path, label] of [
      ['/migrations/mig-5d7e2b4a12', 'Migration sections'],
      ['/plans/plan-c81d44a0', 'Plan sections'],
    ]) {
      await page.goto(path!);
      const nav = page.getByRole('navigation', { name: label! });
      await expect(nav).toBeVisible();
      for (const link of await nav.getByRole('link').all()) {
        const hash = (await link.getAttribute('href'))!;
        expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(44);
        await link.focus();
        await page.keyboard.press('Enter');
        await expect(page.locator(hash)).toBeFocused();
        expect((await page.locator(hash).boundingBox())!.y).toBeGreaterThanOrEqual((await nav.boundingBox())!.y + (await nav.boundingBox())!.height);
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      }
      await page.reload();
      const hash = new URL(page.url()).hash;
      await expect(page.locator(hash)).toBeFocused();
      // Repeating a native fragment link must still land after manually scrolling away.
      await page.evaluate(() => window.scrollTo(0, 0));
      await nav.locator(`a[href="${hash}"]`).click();
      await expect(page.locator(hash)).toBeFocused();
    }
  });
}

test('search and select controls keep 44px touch targets in both themes', async ({ page }) => {
  await signIn(page, '/plans');
  for (const width of [320, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.className = value; }, theme);
      for (const control of [page.getByLabel('Search plans'), page.getByLabel('Status', { exact: true })]) {
        expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    }
  }
});

for (const width of [320, 768, 1440]) {
  test(`VM selection stays reviewable, touchable and keyboard-safe at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await signIn(page, '/inventory/rhosp17-dc1');
    const search = page.getByLabel('Search VMs');
    const selectedOnly = page.getByRole('checkbox', { name: 'Selected only', exact: true });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.className = value; }, theme);
      for (const name of ['Select web-01', 'Select db-pg-01']) {
        const checkbox = page.getByRole('checkbox', { name, exact: true });
        const target = checkbox.locator('..');
        const box = (await target.boundingBox())!;
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.width).toBeGreaterThanOrEqual(44);
        await target.click({ position: { x: 2, y: box.height / 2 } });
        await expect(checkbox).toBeChecked();
        await checkbox.focus();
        await page.keyboard.press('Space');
        await expect(checkbox).not.toBeChecked();
        await target.click();
        await expect(checkbox).toBeChecked();
      }
      await search.fill('db-pg-01');
      await expect(page.getByRole('status').filter({ hasText: '2 selected (1 not shown)' })).toBeVisible();
      await selectedOnly.check();
      await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
      await expect(search).toBeFocused();
      await selectedOnly.check();
      const table = page.getByRole('table', { name: /^VMs on/ });
      await expect(table.getByRole('row')).toHaveCount(3);
      await page.getByRole('checkbox', { name: 'Select web-01', exact: true }).click();
      await expect(search).toBeFocused();
      await expect(table.getByRole('row')).toHaveCount(2);
      await page.getByRole('button', { name: 'Clear selection', exact: true }).click();
      await expect(search).toBeFocused();
      await expect(selectedOnly).not.toBeChecked();
      await expect(page.getByRole('button', { name: 'Create plan', exact: true })).toHaveAttribute('aria-disabled', 'true');
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    }
  });
}

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
