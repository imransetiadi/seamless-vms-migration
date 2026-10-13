import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

const AXE = readFileSync(new URL('../node_modules/axe-core/axe.min.js', import.meta.url), 'utf8');

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

test('chart code loads with the overview that draws charts, not with the sign-in page (SDD §16)', async ({ page }) => {
  const scripts: string[] = [];
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(new URL(request.url()).pathname);
  });
  await page.goto('/login');
  await expect(page.getByRole('button', { name: /sign in/i })).toBeVisible();
  await page.waitForLoadState('networkidle');
  expect(scripts.filter((path) => path.includes('/charts-'))).toEqual([]);

  await page.getByLabel(/token/i).fill('admin');
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.locator('.recharts-surface').first()).toBeVisible();
  expect(scripts.filter((path) => path.includes('/charts-'))).toHaveLength(1);
});

test('the fonts come with the dashboard: nothing loads from another host (SDD §16)', async ({ page, baseURL }) => {
  const foreign: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).origin !== new URL(baseURL ?? '').origin) foreign.push(request.url());
  });
  await signIn(page);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await page.waitForLoadState('networkidle');
  const loaded = await page.evaluate(async () => {
    await document.fonts.ready;
    const faces: string[] = [];
    document.fonts.forEach((font) => {
      if (font.status === 'loaded') faces.push(`${font.family.replaceAll('"', '')} ${font.weight}`);
    });
    return faces;
  });
  expect(foreign).toEqual([]);
  expect(loaded).toEqual(expect.arrayContaining(['Fira Sans 400', 'Fira Code 600']));
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
  const dialog = page.getByRole('dialog', { name: /^add provider$/i });
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

test("on a phone the Overview's first downtime clock is on the first screen, under compact key figures (SDD §16)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, 'viewer');
  await page.goto('/');
  const strip = page.getByRole('region', { name: 'Key metrics' });
  await expect(strip.getByRole('group', { name: 'Migrations' })).toBeVisible();
  // every figure and its hint still shown, in less height
  for (const name of ['Migrations', 'In progress', 'Awaiting finalize', 'Finalized', 'Failed', 'Average downtime', 'SLO compliance']) {
    await expect(strip.getByRole('group', { name })).toBeVisible();
  }
  // the first VM that is down shows its running clock and its SLO within the first screen
  const first = page.getByRole('region', { name: 'Downtime now' }).getByRole('listitem').first();
  const slo = first.getByText(/^of \d+:\d\d$/);
  await expect(slo).toBeVisible();
  const box = await slo.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(844);
});

test("the plans list keeps each plan's progress and clouds on a phone, without scrolling sideways (SDD §16)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, 'viewer');
  await page.goto('/plans');
  const table = page.getByRole('table', { name: /migration plans/i });
  await expect(table).toBeVisible();
  const sideways = await page.locator('.table-wrap').first().evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(sideways).toBeLessThanOrEqual(1);
  const row = table.getByRole('row').nth(1);
  await expect(row.getByText(/^\d+\/\d+ done$/)).toBeVisible();
  // the clouds column is hidden on a phone: the same text stands under the name, visible
  await expect(row.getByText(/^RHOSP 17\.1 — DC1 → RHOSO 18\.0 — Production$/).filter({ visible: true })).toHaveCount(1);
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

for (const width of [320, 375]) {
  test(`every target is 24 px or spaced from the next on a ${width} px wide screen (WCAG 2.5.8)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await signIn(page, 'viewer');
    for (const path of ['/', '/plans', '/plans/plan-4f2a9c1e', '/providers', '/inventory/rhosp17-dc1', '/events', '/advisor']) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await expect(page.getByRole('status').filter({ hasText: /loading/i })).toHaveCount(0);
      await page.addScriptTag({ content: AXE });
      const failures = await page.evaluate(async () => {
        const axe = (window as unknown as { axe: { run: (context: Document, options: object) => Promise<{ violations: Array<{ nodes: Array<{ target: string[] }> }> }> } }).axe;
        const result = await axe.run(document, { runOnly: { type: 'rule', values: ['target-size'] } });
        return result.violations.flatMap((v) => v.nodes.map((n) => n.target.join(' ')));
      });
      expect(failures, `${path}: ${failures.length} targets too small and too close`).toEqual([]);
    }
  });
}

test('the error summaries of the plan, policy and provider dialogs keep their links 24 px or spaced (WCAG 2.5.8)', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await signIn(page, 'admin');
  const targets = async () => {
    await page.addScriptTag({ content: AXE });
    return page.evaluate(async () => {
      const axe = (window as unknown as { axe: { run: (context: Element, options: object) => Promise<{ violations: Array<{ nodes: Array<{ target: string[] }> }> }> } }).axe;
      const dialog = document.querySelector('[role="dialog"]') as Element;
      const result = await axe.run(dialog, { runOnly: { type: 'rule', values: ['target-size'] } });
      return result.violations.flatMap((v) => v.nodes.map((n) => n.target.join(' ')));
    });
  };

  await page.goto('/plans');
  await page.getByRole('button', { name: 'New plan' }).click();
  const plan = page.getByRole('dialog', { name: 'New migration plan' });
  await plan.getByRole('button', { name: /^create plan/i }).click();
  await expect(plan.getByRole('alert').first()).toBeVisible();
  expect(await targets(), 'plan dialog').toEqual([]);
  await page.keyboard.press('Escape');

  await page.goto('/advisor/policies');
  await page.getByRole('button', { name: 'New policy' }).click();
  const policy = page.getByRole('dialog', { name: 'New policy' });
  await policy.getByRole('button', { name: 'Create policy' }).click();
  await expect(policy.getByRole('alert').first()).toBeVisible();
  expect(await targets(), 'policy dialog').toEqual([]);
  await page.keyboard.press('Escape');

  await page.goto('/providers');
  await page.getByRole('button', { name: /^add provider$/i }).first().click();
  const provider = page.getByRole('dialog', { name: /^add provider$/i });
  await provider.getByRole('button', { name: /^add provider$/i }).click();
  await expect(provider.getByRole('alert').first()).toBeVisible();
  expect(await targets(), 'provider dialog').toEqual([]);
});

// With the web fonts and with the system font a browser shows until they arrive (font-display: swap), or when they
// cannot load: a name that fits in Fira Sans can be a few pixels too wide in the system font. Each run measures once
// its fonts are settled.
for (const [width, fonts] of [
  [320, 'web'],
  [1280, 'web'],
  [320, 'fallback'],
  [1280, 'fallback'],
] as const) {
  test(`no text is cut off with an ellipsis on a ${width} px wide screen${fonts === 'fallback' ? ' without the web fonts' : ''} (WCAG 1.4.10, SDD §16)`, async ({ page }) => {
    if (fonts === 'fallback') await page.route(/\.woff2?$/, (route) => route.abort());
    await page.setViewportSize({ width, height: 800 });
    await signIn(page, 'viewer');
    for (const path of ['/', '/plans', '/plans/plan-4f2a9c1e', '/providers', '/inventory/rhosp17-dc1', '/events', '/advisor']) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await expect(page.getByRole('status').filter({ hasText: /loading/i })).toHaveCount(0);
      await page.evaluate(() => document.fonts.ready);
      const cut = await page.evaluate(() =>
        Array.from(document.querySelectorAll<HTMLElement>('body *'))
          .filter((el) => getComputedStyle(el).textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1 && el.getClientRects().length > 0)
          .map((el) => (el.textContent ?? '').trim().slice(0, 50)),
      );
      expect(cut, `${path}: text cut off`).toEqual([]);
    }
  });
}
