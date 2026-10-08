#!/usr/bin/env node
// browser-demo.mjs - real-browser smoke of the running Seamless Migrate demo stack (QASuite §14.7).
//
//   make seamless-demo
//   TOKEN_FILE=<file holding the admin token> node tests/e2e/browser-demo.mjs   # BASE defaults to http://127.0.0.1:8080
//
// Opens the dashboard in headless Chromium through the control plane (so the security headers, the
// CSP and the real API apply), signs in, visits every page and the first plan and warm migration,
// and fails on any console error, page error, CSP violation, failed non-API request, or a page
// without its charts. Uses the Playwright already installed for the dashboard e2e suite
// (`npm --prefix dashboard ci && npx --prefix dashboard playwright install chromium`).
// Never prints the token. Exit code 1 on any problem.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../dashboard/package.json', import.meta.url));
const { chromium } = require('playwright');

const tokenFile = process.env.TOKEN_FILE;
if (!tokenFile) {
  console.error('TOKEN_FILE=<file holding the admin token> is required (the token is never passed on the command line)');
  process.exit(2);
}
const token = readFileSync(tokenFile, 'utf8').trim();
const base = (process.env.BASE ?? 'http://127.0.0.1:8080').replace(/\/$/, '');

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const problems = [];
page.on('console', (msg) => {
  if (msg.type() === 'error') problems.push(`console: ${msg.text()}`);
});
page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
page.on('response', (res) => {
  if (res.status() >= 400 && !res.url().includes('/api/v1/')) problems.push(`http ${res.status()} ${res.url()}`);
});

const charts = () => page.locator('svg.recharts-surface');
const expectCharts = async (name, min) => {
  try {
      await charts().nth(min - 1).waitFor({ state: 'attached', timeout: 10_000 });
    } catch {
      problems.push(`${name}: expected at least ${min} chart(s), found ${await charts().count()}`);
    }
  };

  try {
  await page.goto(`${base}/login`, { waitUntil: 'load' });
  await page.getByLabel(/token/i).fill(token);
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL((u) => !u.pathname.endsWith('/login'));
  await expectCharts('overview', 2);
  for (const path of ['/plans', '/providers', '/inventory', '/events', '/advisor']) {
    await page.goto(`${base}${path}`, { waitUntil: 'load' });
    await page.getByRole('heading', { level: 1 }).waitFor({ timeout: 10_000 });
  }
  await page.goto(`${base}/plans`, { waitUntil: 'load' });
  await page.locator('a[href^="/plans/plan-"]').first().click();
  await page.waitForURL(/\/plans\/plan-/);
  await page.locator('a[href^="/migrations/mig-"]').first().click();
  await page.waitForURL(/\/migrations\/mig-/);
  await page.getByRole('heading', { name: /estimate inputs/i }).waitFor({ timeout: 10_000 });
  // a warm migration shows the sync-pass chart once it has passes (an empty state before)
  const warmPanel = page.getByRole('heading', { name: /sync-pass convergence/i });
  if ((await warmPanel.count()) > 0 && (await page.getByText(/no sync passes yet/i).count()) === 0) await expectCharts('migration', 1);

  // Swagger UI (public in demo mode) must boot under its nonce-based CSP: the operations list
  // renders only when the inline bootstrap script was allowed (SDD §12)
  await page.goto(`${base}/api/docs`, { waitUntil: 'load' });
  try {
    await page.locator('#swagger-ui .opblock-tag-section, #swagger-ui .opblock').first().waitFor({ timeout: 15_000 });
  } catch {
    problems.push('api docs: Swagger UI did not render (CSP blocked its bootstrap script?)');
  }

  await page.goBack({ waitUntil: 'load' });
  await page.getByRole('heading', { name: /estimate inputs/i }).waitFor({ timeout: 10_000 });
  const font = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  if (!/Fira Sans/.test(font)) problems.push(`font stack not applied: ${font}`);
  if (bg !== 'rgb(15, 23, 42)') problems.push(`dark background token not applied: ${bg}`);
  console.log(JSON.stringify({ base, font, background: bg, problems }, null, 1));
} catch (err) {
  problems.push(`journey aborted: ${err instanceof Error ? err.message.split('\n')[0] : String(err)}`);
  console.log(JSON.stringify({ base, problems }, null, 1));
} finally {
  await browser.close();
}
process.exit(problems.length ? 1 : 0);
