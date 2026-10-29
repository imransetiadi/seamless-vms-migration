#!/usr/bin/env node
// demo-keyboard-browser.mjs - planning and starting by keyboard alone (QASuite UI-03, UI-10; WCAG 2.1.1, 2.4.1, 2.4.3,
// 2.4.7), on a throwaway demo server of this checkout.
//
//   npm --prefix dashboard run build                 # the server serves dashboard/dist
//   node tests/e2e/demo-keyboard-browser.mjs         # needs seamless/.venv and the dashboard's Playwright
//
// Creates a throwaway operator token with `seamless token create` in its own data directory (it is never printed)
// and a plan of the free finance VMs report-01 and batch-01 through the API, then starts `seamless serve --demo` on
// 127.0.0.1 with authentication on, a free port and a clean environment. In headless Chromium only Tab, Shift+Tab,
// Enter and Escape move or act: the operator signs in; the first Tab stop skips to the content; the rail leads to
// Plans; New plan's dialog takes the focus, keeps it and gives it back to New plan on Escape; the plan opens from
// the list; Validate and Start run, Start's confirmation taking the focus; the focus is never lost to the page; and
// every stop is visible with a focus indicator. SEAMLESS (the seamless command), PORT and SHOT_DIR (a screenshot of
// the page when a check fails) override the defaults. Exit code = number of failed checks.
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
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

async function unauthenticated(path) {
  try {
    return (await fetch(`${base}/api/v1${path}`, { signal: AbortSignal.timeout(5_000) })).status;
  } catch {
    return null;
  }
}

// another server on the port would be planned on instead - a shared demo would lose its VMs
if ((await unauthenticated('/health')) !== null) {
  console.log(`STOP  something already answers on 127.0.0.1:${port}: pick a free PORT`);
  process.exit(1);
}
// none of the caller's settings reach the server: SQLite and the token in its own directory
const data = mkdtempSync(join(tmpdir(), 'seamless-keyboard-browser.'));
const env = { PATH: process.env.PATH, HOME: process.env.HOME, SEAMLESS_DATA_DIR: data };
if (process.env.PYTHONPATH) env.PYTHONPATH = process.env.PYTHONPATH;

/** A throwaway token: the first line `seamless token create` prints, its YAML entry the rest. */
function token(name, role) {
  const made = spawnSync(seamless, ['token', 'create', '--name', name, '--role', role], { env, encoding: 'utf8' });
  if (made.status !== 0) throw new Error(`seamless token create failed: ${made.stderr.trim().split('\n').pop()}`);
  const [secret, , ...entry] = made.stdout.trim().split('\n');
  return { secret: secret.trim(), entry: entry.join('\n') };
}
let operator;
try {
  operator = token('keyboard-operator', 'operator');
} catch (error) {
  rmSync(data, { recursive: true, force: true });
  console.log(`STOP  ${error.message}`);
  process.exit(1);
}
writeFileSync(join(data, 'tokens.yaml'), `tokens:\n${operator.entry}\n`, { mode: 0o600 });
const log = openSync(join(data, 'server.log'), 'w');
const server = spawn(seamless, ['serve', '--demo', '--host', '127.0.0.1', '--port', port], {
  env: {
    ...env,
    SEAMLESS_DATA_DIR: data,
    SEAMLESS_TOKENS_FILE: join(data, 'tokens.yaml'),
    SEAMLESS_AUTH_DISABLED: 'false',
    SEAMLESS_DEMO_FAILURE_RATE: '0',
    SEAMLESS_DEMO_SPEED: '60',
  },
  stdio: ['ignore', log, log],
});
let exited = false;
server.on('exit', () => { exited = true; });
server.on('error', () => { exited = true; });
let browser = null;

async function api(path, init = {}) {
  try {
    const response = await fetch(`${base}/api/v1${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${operator.secret}`, 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(30_000),
    });
    return { status: response.status, body: response.ok ? await response.json() : null };
  } catch {
    return { status: 0, body: null };
  }
}

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
  const health = await until(async () => (exited ? { status: 'exited' } : await fetch(`${base}/api/v1/health`).then((r) => r.json()).catch(() => null)), (h) => h.status === 'ok' || h.status === 'exited', 60);
  // healthy, and the answer is this server's: it did not exit (on a port taken meanwhile, for one)
  if (health?.status === 'ok' && health.demo === true && !exited) ok(`throwaway demo server healthy on 127.0.0.1:${port}`);
  else {
    bad('throwaway demo server healthy', readFileSync(join(data, 'server.log'), 'utf8').split('\n').slice(-5).join(' | '));
    await finish('the demo server did not start');
  }
  const index = await fetch(`${base}/`).then((r) => r.text()).catch(() => '');
  if (!index.includes('id="root"')) {
    bad('the server serves the dashboard', 'no dashboard/dist: run npm --prefix dashboard run build');
    await finish('nothing to drive');
  }
  // the plan the run validates and starts, made through the API: the run is about driving it by keyboard
  const inventory = (await api('/providers/rhosp17-finance/inventory')).body;
  const vms = Array.isArray(inventory) ? inventory : (inventory?.vms ?? []);
  const ids = ['report-01', 'batch-01'].map((name) => vms.find((v) => v.name === name)?.source_id);
  const created = await api('/plans', {
    method: 'POST',
    body: JSON.stringify({
      name: 'Keyboard run',
      source_provider_id: 'rhosp17-finance',
      destination_provider_id: 'rhoso18',
      vm_ids: ids,
      default_strategy: 'cold',
      mappings: { networks: { 'finance-app': 'finance-app', 'finance-db': 'finance-db' }, volume_types: { 'ceph-ssd': 'ceph-ssd', 'ceph-hdd': 'ceph-hdd' } },
    }),
  });
  const planId = created.body?.id;
  if (created.status === 201 && planId) ok('a plan of report-01 and batch-01 to drive');
  else {
    bad('a plan of report-01 and batch-01 to drive', `POST /plans answered ${created.status}`);
    await finish('nothing to drive');
  }

  browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let shots = 0;
  /** A check that reads the page: a failure keeps a screenshot, and a failure the rest builds on stops. */
  const check = async (what, fn, { needed = false } = {}) => {
    try {
      await fn();
      ok(what);
    } catch (error) {
      bad(what, String(error.message).split('\n')[0]);
      shots += 1;
      if (process.env.SHOT_DIR) await page.screenshot({ path: join(process.env.SHOT_DIR, `demo-keyboard-${shots}.png`), fullPage: true });
      if (needed) await finish('nothing left to check');
    }
  };
  /** The focused element: what it is called, whether it shows, has a focus indicator and sits in a dialog. */
  const focused = () =>
    page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return { name: '(the page)', tag: 'BODY', visible: false, ring: false, inDialog: false };
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const ring = (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0) || (style.boxShadow !== '' && style.boxShadow !== 'none');
      const label = el.getAttribute('aria-label') || el.labels?.[0]?.textContent || el.textContent || el.getAttribute('name') || '';
      return {
        name: label.replace(/\s+/g, ' ').trim().slice(0, 60),
        tag: el.tagName,
        visible: box.width > 0 && box.height > 0 && style.visibility !== 'hidden',
        ring,
        inDialog: Boolean(el.closest('[role="dialog"], [role="alertdialog"]')),
      };
    });
  const unseen = new Set();
  /** Tab (or Shift+Tab) until the focus reaches a control named `match`, noting every stop it was not seen at. */
  const tabTo = async (match, max = 80, key = 'Tab') => {
    for (let i = 1; i <= max; i += 1) {
      await page.keyboard.press(key);
      const f = await focused();
      if (!f.visible || !f.ring) unseen.add(`${f.tag} "${f.name}"${f.visible ? '' : ' (hidden)'}${f.ring ? '' : ' (no focus indicator)'}`);
      if (match.test(f.name)) return f;
    }
    throw new Error(`the focus never reached ${match} in ${max} presses of ${key}`);
  };

  await check('the operator signs in by keyboard', async () => {
    await page.goto(`${base}/login`);
    await page.getByRole('heading', { level: 1, name: 'Sign in' }).waitFor({ timeout: 15_000 });
    await tabTo(/^API token$/, 10);
    await page.keyboard.type(operator.secret);
    await page.keyboard.press('Enter');
    await page.getByRole('heading', { level: 1, name: 'Overview' }).waitFor({ timeout: 15_000 });
  }, { needed: true });

  await check('the first Tab stop skips to the content (WCAG 2.4.1)', async () => {
    await page.keyboard.press('Tab');
    const f = await focused();
    if (!/skip/i.test(f.name)) throw new Error(`it is ${f.tag} "${f.name}"`);
  });

  await check('the rail leads to Plans', async () => {
    await tabTo(/^Plans$/, 40);
    await page.keyboard.press('Enter');
    await page.getByRole('heading', { level: 1, name: 'Plans' }).waitFor({ timeout: 15_000 });
  }, { needed: true });

  await check("New plan's dialog takes the focus, keeps it, and Escape gives it back to New plan", async () => {
    await tabTo(/^New plan$/, 40);
    await page.keyboard.press('Enter');
    await page.getByRole('dialog').waitFor({ timeout: 10_000 });
    await page.waitForTimeout(300);
    if (!(await focused()).inDialog) throw new Error('the dialog did not take the focus');
    for (let i = 0; i < 120; i += 1) {
      await page.keyboard.press('Tab');
      const f = await focused();
      if (!f.inDialog) throw new Error(`the focus left the open dialog for ${f.tag} "${f.name}"`);
    }
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 5_000 });
    const back = await focused();
    if (back.name !== 'New plan') throw new Error(`the focus went to ${back.tag} "${back.name}"`);
  });

  await check('the plan opens from the list', async () => {
    await tabTo(/^Keyboard run$/, 80);
    await page.keyboard.press('Enter');
    await page.getByRole('heading', { level: 1, name: 'Keyboard run' }).waitFor({ timeout: 15_000 });
  }, { needed: true });

  await check('Validate runs by keyboard, the focus staying on the page', async () => {
    await tabTo(/^Validate$/, 60);
    await page.keyboard.press('Enter');
    if (!(await until(async () => (await api(`/plans/${planId}`)).body, (p) => p.status === 'validated', 60))) throw new Error('the plan was not validated');
    await page.waitForTimeout(1000);
    const f = await focused();
    if (f.tag === 'BODY') throw new Error('the focus was lost to the page');
  }, { needed: true });

  await check('Start runs by keyboard: its confirmation takes the focus, then the heading has it', async () => {
    await tabTo(/^Start$/, 60);
    await page.keyboard.press('Enter');
    await page.getByRole('alertdialog').waitFor({ timeout: 10_000 });
    await page.waitForTimeout(300);
    if (!(await focused()).inDialog) throw new Error('the confirmation did not take the focus');
    await tabTo(/^Start plan$/, 10);
    await page.keyboard.press('Enter');
    if (!(await until(async () => (await api(`/plans/${planId}`)).body, (p) => p.status === 'running', 30))) throw new Error('the plan did not start');
    await page.waitForTimeout(1000);
    // Start goes with the action: the focus moves to the plan's heading, never to the page (SDD §16)
    const f = await focused();
    if (f.tag !== 'H1' || f.name !== 'Keyboard run') throw new Error(`the focus is on ${f.tag} "${f.name}"`);
  });

  if (unseen.size) bad('every Tab stop was visible, with a focus indicator (WCAG 2.4.7)', [...unseen].slice(0, 6).join(' | '));
  else ok('every Tab stop was visible, with a focus indicator (WCAG 2.4.7)');
  if (errors.length) bad('no uncaught errors in the page', errors.slice(0, 3).join(' | '));
  else ok('no uncaught errors in the page');
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
