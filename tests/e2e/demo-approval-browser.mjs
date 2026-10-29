#!/usr/bin/env node
// demo-approval-browser.mjs - maker-checker from the dashboard alone (SDD §5.4, §16, ADV-17), on a throwaway
// demo server of this checkout.
//
//   npm --prefix dashboard run build                 # the server serves dashboard/dist
//   node tests/e2e/demo-approval-browser.mjs         # needs seamless/.venv and the dashboard's Playwright
//
// Creates two throwaway tokens - a requester (admin) and an approver - with `seamless token create` in its
// own data directory (the tokens are never printed), and starts `seamless serve --demo` on 127.0.0.1 with
// authentication on, a free port and a clean environment, so a shared demo keeps its VMs. In two headless
// Chromium sessions every action is a person's own: the requester plans the free finance VMs report-01 and
// batch-01 through New plan with the dialog's defaults (Require approval, a distinct approver), validates,
// starts and requests each cutover - the dialog asks to request it, not to start it - and cannot approve
// it; the approver approves - its dialog says the approval starts the cutover and stops the source VM - and
// each cutover runs and verifies; while report-01's source VM runs again (the demo's power button) Finalize is
// unavailable, saying why; the approver finalizes report-01, typing its name, and rolls back batch-01.
// The API is read only to wait for a phase. SEAMLESS (the seamless command), PORT and SHOT_DIR (a screenshot
// of the page when a check fails) override the defaults.
// Exit code = number of failed checks.
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
// none of the caller's settings reach the server: SQLite and the tokens in its own directory
const data = mkdtempSync(join(tmpdir(), 'seamless-approval-browser.'));
const env = { PATH: process.env.PATH, HOME: process.env.HOME, SEAMLESS_DATA_DIR: data };
if (process.env.PYTHONPATH) env.PYTHONPATH = process.env.PYTHONPATH;

/** A throwaway token: the first line `seamless token create` prints, its YAML entry the rest. */
function token(name, role) {
  const made = spawnSync(seamless, ['token', 'create', '--name', name, '--role', role], { env, encoding: 'utf8' });
  if (made.status !== 0) throw new Error(`seamless token create failed: ${made.stderr.trim().split('\n').pop()}`);
  const [secret, , ...entry] = made.stdout.trim().split('\n');
  return { secret: secret.trim(), entry: entry.join('\n') };
}
let requester;
let approver;
try {
  requester = token('approval-requester', 'admin');
  approver = token('approval-approver', 'approver');
} catch (error) {
  rmSync(data, { recursive: true, force: true });
  console.log(`STOP  ${error.message}`);
  process.exit(1);
}
writeFileSync(join(data, 'tokens.yaml'), `tokens:\n${requester.entry}\n${approver.entry}\n`, { mode: 0o600 });
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

async function json(path) {
  try {
    const response = await fetch(`${base}/api/v1${path}`, { headers: { Authorization: `Bearer ${requester.secret}` }, signal: AbortSignal.timeout(10_000) });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
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
  if ((await unauthenticated('/plans')) === 401) ok('authentication is on: a request without a token is refused');
  else {
    bad('authentication is on');
    await finish('maker-checker needs two users');
  }
  const index = await fetch(`${base}/`).then((r) => r.text()).catch(() => '');
  if (!index.includes('id="root"')) {
    bad('the server serves the dashboard', 'no dashboard/dist: run npm --prefix dashboard run build');
    await finish('nothing to drive');
  }

  browser = await chromium.launch();
  const signIn = async (secret) => {
    const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage();
    await page.goto(`${base}/login`);
    await page.getByLabel('API token').fill(secret);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 15_000 });
    return page;
  };
  const asRequester = await signIn(requester.secret);
  const asApprover = await signIn(approver.secret);
  let shots = 0;
  /** A check that reads a page: a failure keeps a screenshot, and a failure the rest builds on stops. */
  const check = async (what, page, fn, { needed = false } = {}) => {
    try {
      await fn();
      ok(what);
    } catch (error) {
      bad(what, String(error.message).split('\n')[0]);
      shots += 1;
      if (process.env.SHOT_DIR) await page.screenshot({ path: join(process.env.SHOT_DIR, `demo-approval-${shots}.png`), fullPage: true });
      if (needed) await finish('nothing left to check');
    }
  };

  let planId = null;
  await check('New plan of report-01 and batch-01, Cold, with the dialog\'s approval defaults', asRequester, async () => {
    await asRequester.goto(`${base}/plans`);
    await asRequester.getByRole('button', { name: 'New plan' }).first().click();
    const dialog = asRequester.getByRole('dialog');
    await dialog.getByLabel('Name').fill('Finance with maker-checker');
    const pick = async (label, pattern) => {
      const select = dialog.getByLabel(label);
      const option = (await select.locator('option').allTextContents()).find((o) => pattern.test(o));
      if (!option) throw new Error(`${label} offers nothing matching ${pattern}`);
      await select.selectOption({ label: option });
    };
    await pick('Source provider', /finance|rhosp/i);
    await pick('Destination provider', /rhoso/i);
    await dialog.getByLabel('Select report-01').check({ timeout: 15_000 });
    await dialog.getByLabel('Select batch-01').check();
    await pick('Default strategy', /^cold$/i);
    if (!(await dialog.getByLabel('Require approval').isChecked()) || !(await dialog.getByLabel('Require a distinct approver').isChecked())) {
      throw new Error('the dialog no longer defaults to an approval by a distinct approver');
    }
    await dialog.locator('summary', { hasText: 'Advanced' }).click();
    await dialog.getByLabel('Network mappings').fill('finance-app = finance-app\nfinance-db = finance-db');
    await dialog.getByLabel('Volume type mappings').fill('ceph-ssd = ceph-ssd\nceph-hdd = ceph-hdd');
    await dialog.getByRole('button', { name: /^Create plan/ }).click();
    await asRequester.getByRole('heading', { level: 1, name: 'Finance with maker-checker' }).waitFor({ timeout: 15_000 });
    planId = asRequester.url().split('/plans/')[1]?.split(/[?#]/)[0];
  }, { needed: true });

  await check('Validate, then Start', asRequester, async () => {
    await asRequester.getByRole('button', { name: 'Validate' }).first().click();
    await asRequester.getByRole('button', { name: /^Start/ }).first().click({ timeout: 30_000 });
    const confirm = asRequester.getByRole('alertdialog');
    if (await confirm.isVisible().catch(() => false)) await confirm.getByRole('button', { name: /^Start/ }).click();
    if (!(await until(() => json(`/plans/${planId}`), (p) => p.status === 'running', 30))) throw new Error('the plan is not running');
  }, { needed: true });

  const migration = async (name) => ((await json(`/migrations?plan_id=${planId}`)) ?? []).find((m) => m.vm.name === name) ?? null;
  const open = async (page, name) => {
    const m = await until(() => migration(name), (x) => x.phase !== 'pending' && x.phase !== 'validating', 60);
    await page.goto(`${base}/migrations/${m.id}`);
    await page.getByRole('heading', { level: 1, name }).waitFor({ timeout: 15_000 });
    return m;
  };
  const cutOver = async (name) => {
    if (!(await until(() => migration(name), (x) => x.phase === 'ready', 60))) throw new Error(`${name} never became ready`);
    await open(asRequester, name);
    await asRequester.getByRole('button', { name: 'Cut over', exact: true }).first().click();
    const dialog = asRequester.getByRole('alertdialog', { name: `Request the cutover of ${name}?` });
    await dialog.waitFor({ timeout: 10_000 });
    await dialog.getByRole('button', { name: 'Request cutover' }).click();
    await asRequester.getByText('Cutover requested by approval-requester').first().waitFor({ timeout: 15_000 });
  };
  const approve = async (name) => {
    await open(asApprover, name);
    await asApprover.getByRole('button', { name: 'Approve', exact: true }).first().click();
    // the approval the cutover waits for starts it: the dialog says the source VM stops (SDD §5.4, §16)
    const dialog = asApprover.getByRole('alertdialog', { name: `Approve the cutover of ${name}?` });
    await dialog.waitFor({ timeout: 10_000 });
    const says = (await dialog.textContent()) ?? '';
    if (!says.includes('Your approval lets the cutover start: the source VM will then be stopped')) throw new Error(`the Approve dialog says ${JSON.stringify(says.slice(0, 160))}`);
    await dialog.getByRole('button', { name: 'Approve and cut over', exact: true }).click();
    const done = await until(() => migration(name), (x) => ['completed', 'failed', 'rolled_back'].includes(x.phase), 180);
    if (done?.phase !== 'completed') throw new Error(`${name} ended ${done?.phase ?? 'nowhere'}: ${done?.error ?? ''}`);
    const tcp = (done.verification?.checks ?? []).find((c) => c.name === 'tcp:22');
    if (!tcp?.ok) throw new Error(`its verification's tcp:22 check: ${JSON.stringify(tcp)}`);
  };

  await check('report-01: the requester asks to request the cutover, not to start it (SDD §16)', asRequester, () => cutOver('report-01'), { needed: true });
  await check("the requester cannot approve their own request, and is told why (maker-checker)", asRequester, async () => {
    const button = asRequester.getByRole('button', { name: 'Approve', exact: true }).first();
    if ((await button.getAttribute('aria-disabled')) !== 'true') throw new Error('Approve is available to the requester');
    const why = await button.evaluate((el) => document.getElementById(el.getAttribute('aria-describedby') ?? '')?.textContent ?? el.title ?? '');
    if (!why.includes('You requested this cutover. Another approver must approve it.')) throw new Error(`it says ${JSON.stringify(why)}`);
  });
  await check('report-01: the approver is told the approval starts the cutover, approves, and it runs and verifies on TCP 22', asApprover, () => approve('report-01'));
  await check('report-01: while its source VM runs again Finalize is unavailable, saying why, and is offered once it is stopped (SDD §8.1)', asApprover, async () => {
    await open(asApprover, 'report-01');
    await asApprover.getByRole('button', { name: 'Power on the source VM (demo)' }).click();
    // the source watch looks every SEAMLESS_SOURCE_WATCH_S, divided by the demo speed: 5 s here
    const alert = asApprover.getByRole('alert', { name: 'Source VM running' });
    await alert.waitFor({ timeout: 30_000 });
    const finalize = asApprover.getByRole('button', { name: 'Finalize', exact: true }).first();
    // an available button carries no aria-disabled at all
    const disabled = async () => (await finalize.getAttribute('aria-disabled')) ?? 'false';
    await until(disabled, (v) => v === 'true', 10);
    if ((await disabled()) !== 'true') throw new Error('Finalize is available while the source VM runs again');
    const why = await finalize.evaluate((el) => document.getElementById(el.getAttribute('aria-describedby') ?? '')?.textContent ?? '');
    if (!why.includes('The source VM runs again: stop it, or roll it back, before finalizing.')) throw new Error(`it says ${JSON.stringify(why)}`);
    await asApprover.getByRole('button', { name: 'Power off the source VM (demo)' }).click();
    await alert.waitFor({ state: 'detached', timeout: 30_000 });
    if (!(await until(disabled, (v) => v !== 'true', 10))) throw new Error('Finalize stays unavailable once the source VM is stopped');
  });
  await check('report-01: Finalize is refused until its name is typed, then the approver finalizes it (QASuite UI-05)', asApprover, async () => {
    await open(asApprover, 'report-01');
    await asApprover.getByRole('button', { name: 'Finalize', exact: true }).first().click();
    const dialog = asApprover.getByRole('alertdialog');
    const confirm = dialog.getByRole('button', { name: 'Finalize' });
    const refused = async (when) => {
      if ((await confirm.getAttribute('aria-disabled')) !== 'true') throw new Error(`Finalize is offered ${when}`);
      const why = await confirm.evaluate((el) => (el.getAttribute('aria-describedby') ?? '').split(' ').map((id) => document.getElementById(id)?.textContent ?? '').join(' '));
      if (!why.includes('Type report-01 to confirm.')) throw new Error(`${when}, it says ${JSON.stringify(why)}`);
    };
    await refused('before the name is typed');
    await dialog.getByLabel(/Type .* to confirm/).fill('report-0');
    await refused('with half the name typed');
    await dialog.getByLabel(/Type .* to confirm/).fill('report-01');
    await confirm.click();
    if (!(await until(() => migration('report-01'), (x) => x.phase === 'finalized', 60))) throw new Error('report-01 was not finalized');
  });
  await check('batch-01: requested by the requester, approved by the approver, completed', asApprover, async () => {
    await cutOver('batch-01');
    await approve('batch-01');
  });
  await check('batch-01: the approver rolls it back, with the reason it asks for, and its source runs again', asApprover, async () => {
    await open(asApprover, 'batch-01');
    await asApprover.getByRole('button', { name: 'Roll back', exact: true }).first().click();
    const dialog = asApprover.getByRole('alertdialog');
    const confirm = dialog.getByRole('button', { name: 'Roll back' });
    if ((await confirm.getAttribute('aria-disabled')) !== 'true') throw new Error('Roll back is offered without a reason');
    await dialog.getByLabel('Reason').fill('The application owner keeps it on the old cloud');
    await confirm.click();
    const done = await until(() => migration('batch-01'), (x) => ['rolled_back', 'failed'].includes(x.phase), 120);
    if (done?.phase !== 'rolled_back') throw new Error(`batch-01 ended ${done?.phase ?? 'nowhere'}: ${done?.error ?? ''}`);
  });
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
