#!/usr/bin/env node
// demo-cutover-refusal-browser.mjs - DEMO-07 (QASuite §6.6) from the dashboard alone, on a throwaway demo
// server of this checkout.
//
//   npm --prefix dashboard run build                    # the server serves dashboard/dist
//   node tests/e2e/demo-cutover-refusal-browser.mjs     # needs seamless/.venv and the dashboard's Playwright
//
// Starts `seamless serve --demo` on 127.0.0.1 as demo-cutover-refusal.sh does - a free port, its own data
// directory and a clean environment - so a shared demo keeps dc2-portal-02 free (one VM, one migration across
// plans, SDD §5.4); a loopback demo needs no token. In headless Chromium every action is the operator's own,
// as DEMO-07 has it: New plan (vCenter DC2, dc2-portal-02, VMware warm, the network mapping, no approval),
// Validate, Start; on the migration page Cut over after the first pass, Retry, Sync now, then Cut over again.
// The checks read the page: the warning, a next step that says to fix it and sync with Sync now ahead of Cut
// over, the refused cutover's next step, and how both clear (SDD §7.4, §16). The API is read only to wait for
// a pass or a phase. SEAMLESS (the seamless command), PORT
// and SHOT_DIR (a screenshot of the page when a check fails) override the defaults.
// Exit code = number of failed checks.
import { spawn } from 'node:child_process';
import { mkdtempSync, openSync, readFileSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../../dashboard/package.json', import.meta.url));
const { chromium } = require('playwright');

const root = fileURLToPath(new URL('../..', import.meta.url));
const seamless = process.env.SEAMLESS ?? join(root, 'seamless/.venv/bin/seamless');
const port = process.env.PORT ?? (await freePort());
const base = `http://127.0.0.1:${port}`;
let pass = 0;
let fail = 0;
const ok = (what) => { pass += 1; console.log(`PASS  ${what}`); };
const bad = (what, why) => { fail += 1; console.log(`FAIL  ${what}${why ? `: ${why}` : ''}`); };

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port: free } = server.address();
      server.close(() => resolve(String(free)));
    });
  });
}

async function json(path) {
  try {
    const response = await fetch(`${base}/api/v1${path}`, { signal: AbortSignal.timeout(10_000) });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

/** The value `read` gives once `holds` accepts it, polling for up to `seconds`; null after that. */
async function until(read, holds, seconds) {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {
    const value = await read();
    if (value !== null && holds(value)) return value;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

// another server on the port would be planned on instead - a shared demo would lose dc2-portal-02
if (await json('/health')) {
  console.log(`STOP  something already answers on 127.0.0.1:${port}: pick a free PORT`);
  process.exit(1);
}
// none of the caller's settings reach the server (database, tokens, Jev, memory, keys): SQLite in its own
// directory, and no injected cutover failures (PYTHONPATH passes, to run another tree's code)
const data = mkdtempSync(join(tmpdir(), 'seamless-demo07-browser.'));
const env = { PATH: process.env.PATH, HOME: process.env.HOME, SEAMLESS_DATA_DIR: data, SEAMLESS_DEMO_FAILURE_RATE: '0', SEAMLESS_DEMO_SPEED: '60' };
if (process.env.PYTHONPATH) env.PYTHONPATH = process.env.PYTHONPATH;
const log = openSync(join(data, 'server.log'), 'w');
const server = spawn(seamless, ['serve', '--demo', '--host', '127.0.0.1', '--port', port], { env, stdio: ['ignore', log, log] });
let exited = false;
server.on('exit', () => { exited = true; });
server.on('error', () => { exited = true; });
let browser = null;

async function finish(reason) {
  if (reason) console.log(`STOP  ${reason}`);
  await browser?.close();
  if (!exited) {
    server.kill('SIGTERM');
    await until(async () => exited, (gone) => gone, 15);
  }
  rmSync(data, { recursive: true, force: true });
  console.log(`== ${pass} passed, ${fail} failed ==`);
  process.exit(fail);
}

try {
  const health = await until(() => (exited ? Promise.resolve({ status: 'exited' }) : json('/health')), (h) => h.status === 'ok' || h.status === 'exited', 60);
  // healthy, and the answer is this server's: it did not exit (on a port taken meanwhile, for one)
  if (health?.status === 'ok' && health.demo === true && !exited) ok(`throwaway demo server healthy on 127.0.0.1:${port}`);
  else {
    bad('throwaway demo server healthy', readFileSync(join(data, 'server.log'), 'utf8').split('\n').slice(-5).join(' | '));
    await finish('the demo server did not start');
  }
  const index = await fetch(`${base}/`).then((r) => r.text()).catch(() => '');
  if (index.includes('id="root"')) ok('the server serves the dashboard');
  else {
    bad('the server serves the dashboard', 'no dashboard/dist: run npm --prefix dashboard run build');
    await finish('nothing to drive');
  }

  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const shot = async () => process.env.SHOT_DIR && page.screenshot({ path: join(process.env.SHOT_DIR, `demo07-browser-${pass + fail}.png`), fullPage: true });
  /** A check that reads the page: a failure keeps a screenshot, and a failure the rest builds on stops. */
  const check = async (what, fn, { needed = false } = {}) => {
    try {
      await fn();
      ok(what);
    } catch (error) {
      bad(what, String(error.message).split('\n')[0]);
      await shot();
      if (needed) await finish('nothing left to check');
    }
  };
  const migrationOf = async (planId) => (await json(`/migrations?plan_id=${planId}`))?.[0] ?? null;

  await check('New plan: vCenter DC2, dc2-portal-02, VMware warm, its network mapping, no approval', async () => {
    await page.goto(`${base}/plans`);
    await page.getByRole('button', { name: 'New plan' }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Name').fill('DEMO-07 from the dashboard');
    const pick = async (label, pattern) => {
      const select = dialog.getByLabel(label);
      const option = (await select.locator('option').allTextContents()).find((o) => pattern.test(o));
      if (!option) throw new Error(`${label} offers nothing matching ${pattern}`);
      await select.selectOption({ label: option });
    };
    await pick('Source provider', /vcenter|dc2/i);
    await pick('Destination (RHOSO 18.0)', /rhoso/i);
    await dialog.getByLabel('Select dc2-portal-02').check({ timeout: 15_000 });
    await pick('Default strategy', /vmware warm/i);
    await dialog.getByLabel('Require approval').uncheck();
    await dialog.locator('summary', { hasText: 'Advanced' }).click();
    await dialog.getByLabel('Network mappings').fill('DC2-Prod = dc2-prod');
    await dialog.getByRole('button', { name: /^Create plan/ }).click();
    await page.getByRole('heading', { level: 1, name: 'DEMO-07 from the dashboard' }).waitFor({ timeout: 15_000 });
  }, { needed: true });
  const planId = page.url().split('/plans/')[1]?.split(/[?#]/)[0];

  await check('Validate, then Start', async () => {
    await page.getByRole('button', { name: 'Validate' }).first().click();
    await page.getByRole('button', { name: /^Start/ }).first().click({ timeout: 30_000 });
    const confirm = page.getByRole('alertdialog');
    if (await confirm.isVisible().catch(() => false)) await confirm.getByRole('button', { name: /^Start/ }).click();
    if (!(await until(() => migrationOf(planId), (m) => m.phase !== 'ready' && m.phase !== 'pending', 60))) throw new Error('the migration never started');
  }, { needed: true });

  const note = page.getByRole('note', { name: 'Cutover will refuse' });
  await check('the migration page warns what the cutover will refuse after the first pass', async () => {
    await page.getByRole('link', { name: 'dc2-portal-02' }).first().click();
    await page.getByRole('heading', { level: 1, name: 'dc2-portal-02' }).waitFor({ timeout: 15_000 });
    await note.waitFor({ timeout: 120_000 });
    const text = await note.innerText();
    if (!/port demo-port-1 holds 10\.2\.0\.32/.test(text) || !text.includes("'dc2-prod'")) throw new Error(`the warning reads ${JSON.stringify(text)}`);
  }, { needed: true });

  const buttonOrder = async () => (await page.getByRole('button').allTextContents()).map((t) => t.trim());
  await check('its next step says to fix it and sync, with Sync now ahead of Cut over (SDD §16)', async () => {
    await page.getByText('Converged, but the cutover will refuse what the last pass found.').waitFor({ timeout: 120_000 });
    const names = await buttonOrder();
    if (!(names.indexOf('Sync now') >= 0 && names.indexOf('Sync now') < names.indexOf('Cut over'))) throw new Error(`buttons in order: ${names.join(', ')}`);
  });

  await check('Cut over anyway: refused before the source stops, the next step says so and offers Retry', async () => {
    await page.getByRole('button', { name: 'Cut over', exact: true }).first().click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Start cutover' }).click();
    const m = await until(() => migrationOf(planId), (x) => ['failed', 'verifying', 'completed', 'rolled_back'].includes(x.phase), 120);
    if (m?.phase !== 'failed' || m.downtime_started_at !== null) throw new Error(`it ended ${m?.phase}, downtime ${m?.downtime_started_at}`);
    await page.getByText('Failed: the cutover refused what the passes found, before the source stopped.').waitFor({ timeout: 30_000 });
    if (!(await note.isVisible())) throw new Error('the warning went away');
    const names = await buttonOrder();
    if (!(names.indexOf('Retry') >= 0 && names.indexOf('Retry') < names.indexOf('Roll back'))) throw new Error(`buttons in order: ${names.join(', ')}`);
  });
  await check("Retry: the retry's pass still finds it, and the page keeps the warning", async () => {
    await page.getByRole('button', { name: 'Retry' }).first().click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Retry' }).click();
    const m = await until(() => migrationOf(planId), (x) => x.phase === 'awaiting_cutover' && x.sync_passes.length >= 2, 120);
    if (!m) throw new Error("the retry's pass never came back to the cutover gate");
    await page.getByText('Converged, but the cutover will refuse what the last pass found.').waitFor({ timeout: 30_000 });
    if (!(await note.isVisible())) throw new Error("the warning went away after the retry's pass");
  });
  await check('Sync now: the third pass finds none, and the warning goes away', async () => {
    await page.getByRole('button', { name: 'Sync now' }).first().click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Sync now' }).click();
    const m = await until(() => migrationOf(planId), (x) => x.phase === 'awaiting_cutover' && x.sync_passes.length >= 3, 120);
    if (!m) throw new Error('the third pass never came back to the cutover gate');
    await note.waitFor({ state: 'hidden', timeout: 30_000 });
    await page.getByText('Converged. Next: request the cutover').waitFor({ timeout: 30_000 });
  });

  await check('Cut over: the migration completes, with nothing left to refuse', async () => {
    await page.getByRole('button', { name: 'Cut over', exact: true }).first().click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Start cutover' }).click();
    const m = await until(() => migrationOf(planId), (x) => ['completed', 'failed', 'rolled_back'].includes(x.phase), 240);
    if (m?.phase !== 'completed') throw new Error(`the migration ended ${m?.phase ?? 'nowhere'}`);
    await page.getByText('Completed').first().waitFor({ timeout: 30_000 });
    if (await note.isVisible()) throw new Error('the warning is still shown');
  });
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
