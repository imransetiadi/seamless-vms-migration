#!/usr/bin/env node
// demo-overview-browser.mjs - the Overview's key metrics and phase distribution against GET /stats (QASuite UI-02;
// SDD §12, §16), on a throwaway demo server of this checkout.
//
//   npm --prefix dashboard run build                 # the server serves dashboard/dist
//   node tests/e2e/demo-overview-browser.mjs         # needs seamless/.venv and the dashboard's Playwright
//
// Creates a throwaway viewer token with `seamless token create` in its own data directory (it is never printed) and
// starts `seamless serve --demo` on 127.0.0.1 with authentication on, a free port, a clean environment and half the
// simulated cutovers failing, so some roll back. In headless Chromium the viewer opens the Overview: each key metric
// and each row of the phase distribution comes to equal GET /stats (the page follows the event stream); the Rolled
// back tile counts the migrations that rolled back, in the warning tone; choosing a plan scopes the metrics to its
// statistics. SEAMLESS (the seamless command), PORT and SHOT_DIR (a screenshot of the page when a check fails)
// override the defaults. Exit code = number of failed checks.
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
    await sleep(500);
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

// another server on the port would be read instead
if ((await unauthenticated('/health')) !== null) {
  console.log(`STOP  something already answers on 127.0.0.1:${port}: pick a free PORT`);
  process.exit(1);
}
// none of the caller's settings reach the server: SQLite and the token in its own directory
const data = mkdtempSync(join(tmpdir(), 'seamless-overview-browser.'));
const env = { PATH: process.env.PATH, HOME: process.env.HOME, SEAMLESS_DATA_DIR: data };
if (process.env.PYTHONPATH) env.PYTHONPATH = process.env.PYTHONPATH;

/** A throwaway token: the first line `seamless token create` prints, its YAML entry the rest. */
function token(name, role) {
  const made = spawnSync(seamless, ['token', 'create', '--name', name, '--role', role], { env, encoding: 'utf8' });
  if (made.status !== 0) throw new Error(`seamless token create failed: ${made.stderr.trim().split('\n').pop()}`);
  const [secret, , ...entry] = made.stdout.trim().split('\n');
  return { secret: secret.trim(), entry: entry.join('\n') };
}
let viewer;
try {
  viewer = token('overview-viewer', 'viewer');
} catch (error) {
  rmSync(data, { recursive: true, force: true });
  console.log(`STOP  ${error.message}`);
  process.exit(1);
}
const tokensFile = join(data, 'tokens.yaml');
writeFileSync(tokensFile, `tokens:\n${viewer.entry}\n`, { mode: 0o600 });
const lastLog = () => readFileSync(join(data, 'server.log'), 'utf8').split('\n').slice(-5).join(' | ');

let server = null;
let exited = true;
/** Starts the demo server on the same data directory and port: its health once it answers, null if it did not. */
async function start() {
  const log = openSync(join(data, 'server.log'), 'a');
  exited = false;
  server = spawn(seamless, ['serve', '--demo', '--host', '127.0.0.1', '--port', port], {
    env: {
      ...env,
      SEAMLESS_DATA_DIR: data,
      SEAMLESS_TOKENS_FILE: tokensFile,
      SEAMLESS_AUTH_DISABLED: 'false',
      SEAMLESS_DEMO_FAILURE_RATE: '0.5',
      SEAMLESS_DEMO_SPEED: '60',
    },
    stdio: ['ignore', log, log],
  });
  server.on('exit', () => { exited = true; });
  server.on('error', () => { exited = true; });
  const health = await until(async () => (exited ? { status: 'exited' } : await fetch(`${base}/api/v1/health`).then((r) => r.json()).catch(() => null)), (h) => h.status === 'ok' || h.status === 'exited', 60);
  // healthy, and the answer is this server's: it did not exit (on a port taken meanwhile, for one)
  return health?.status === 'ok' && health.demo === true && !exited ? health : null;
}
async function stop() {
  if (server && !exited) {
    server.kill('SIGTERM');
    await until(async () => exited, (gone) => gone, 15);
  }
  server = null;
}
let browser = null;

async function finish(reason) {
  if (reason) console.log(`STOP  ${reason}`);
  await browser?.close();
  await stop();
  rmSync(data, { recursive: true, force: true });
  console.log(`== ${pass} passed, ${fail} failed ==`);
  process.exit(fail);
}

/** The server's statistics, for all plans or one. */
async function stats(planId = '') {
  try {
    const response = await fetch(`${base}/api/v1/stats${planId ? `?plan_id=${planId}` : ''}`, {
      headers: { Authorization: `Bearer ${viewer.secret}` },
      signal: AbortSignal.timeout(10_000),
    });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

/** The figures the key metrics show for `s`, by tile. */
const expected = (s) => ({
  // a cancelled migration is never done: Migrations counts the others (SDD §16)
  Migrations: s.total - (s.by_phase.cancelled ?? 0),
  'In progress': s.in_progress,
  'Awaiting finalize': s.awaiting_finalize,
  Finalized: s.finalized,
  Failed: s.failed,
  'Rolled back': s.by_phase.rolled_back ?? 0,
});
// the phase distribution's labels (dashboard/src/lib/phase.ts)
const LABELS = {
  pending: 'Pending', validating: 'Validating', blocked: 'Blocked', ready: 'Ready', precopy: 'Pre-copy', syncing: 'Syncing',
  awaiting_cutover: 'Awaiting cutover', cutover: 'Cutover', verifying: 'Verifying', completed: 'Completed', finalized: 'Finalized',
  failed: 'Failed', rolling_back: 'Rolling back', rolled_back: 'Rolled back', cancelled: 'Cancelled',
};

try {
  if (await start()) ok(`throwaway demo server healthy on 127.0.0.1:${port}`);
  else {
    bad('throwaway demo server healthy', lastLog());
    await finish('the demo server did not start');
  }
  const index = await fetch(`${base}/`).then((r) => r.text()).catch(() => '');
  if (!index.includes('id="root"')) {
    bad('the server serves the dashboard', 'no dashboard/dist: run npm --prefix dashboard run build');
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
      if (process.env.SHOT_DIR) await page.screenshot({ path: join(process.env.SHOT_DIR, `demo-overview-${shots}.png`), fullPage: true });
      if (needed) await finish('nothing left to check');
    }
  };
  /** The key metrics as shown: each tile's figure, a number or null (a dash), and whether it is in the warning tone. */
  const tiles = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('[aria-label="Key metrics"] [role="group"]')].map((g) => {
    const value = g.querySelector('.num');
    const text = value?.textContent?.replace(/[^0-9]/g, '') ?? '';
    return [g.getAttribute('aria-label'), { n: text === '' ? null : Number(text), warning: value?.classList.contains('text-status-warning') ?? false }];
  })));
  /** The phase distribution as shown: count by label. */
  const phases = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('ul[aria-label$="migrations by phase"] > li')].map((li) => {
    const spans = li.querySelectorAll(':scope > span');
    // the count's cell also says of how many: its first number is the count
    return [spans[0]?.textContent?.trim() ?? '', Number((spans[2]?.textContent ?? '').match(/\d+/)?.[0] ?? NaN)];
  })));
  /** The tiles that differ from `s`, as "label: shown != expected". */
  const differences = async (s) => {
    const shown = await tiles();
    return Object.entries(expected(s)).filter(([label, n]) => shown[label]?.n !== n).map(([label, n]) => `${label}: ${shown[label]?.n ?? 'missing'} != ${n}`);
  };
  /** Page and server agree: polls both until the tiles equal the statistics read just before them. */
  const agree = async (planId, seconds) => {
    let last = [];
    const end = Date.now() + seconds * 1000;
    while (Date.now() < end) {
      const s = await stats(planId);
      if (s) {
        last = await differences(s);
        if (last.length === 0) return s;
      }
      await sleep(1000);
    }
    throw new Error(last.join('; ') || 'no statistics');
  };

  await check('the viewer signs in and lands on the Overview', async () => {
    await page.goto(`${base}/login`);
    await page.getByLabel('API token').fill(viewer.secret);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.getByRole('heading', { level: 1, name: 'Overview' }).waitFor({ timeout: 15_000 });
    await page.locator('[aria-label="Key metrics"] [role="group"]').first().waitFor({ timeout: 15_000 });
  }, { needed: true });

  await check('every key metric equals GET /stats (UI-02)', async () => {
    await agree('', 30);
  });

  await check('the phase distribution equals GET /stats by phase (UI-02)', async () => {
    let last = '';
    const end = Date.now() + 30_000;
    while (Date.now() < end) {
      const s = await stats();
      const shown = await phases();
      const want = Object.fromEntries(Object.entries(s?.by_phase ?? {}).filter(([, n]) => n > 0).map(([phase, n]) => [LABELS[phase] ?? phase, n]));
      last = `shown ${JSON.stringify(shown)}, the server ${JSON.stringify(want)}`;
      if (s && JSON.stringify(Object.entries(shown).sort()) === JSON.stringify(Object.entries(want).sort())) return;
      await sleep(1000);
    }
    throw new Error(last);
  });

  await check('a migration that rolled back shows in the Rolled back tile, in the warning tone (SDD §16)', async () => {
    // half the simulated cutovers fail and roll back
    if (!(await until(async () => (await stats())?.by_phase.rolled_back ?? 0, (n) => n > 0, 120))) throw new Error('no migration rolled back in 120 s');
    await agree('', 30);
    const tile = (await tiles())['Rolled back'];
    if (!tile?.warning) throw new Error(`the tile reads ${tile?.n} without the warning tone`);
  });

  await check('choosing a plan scopes the key metrics to its statistics (UI-02)', async () => {
    const plans = await fetch(`${base}/api/v1/plans`, { headers: { Authorization: `Bearer ${viewer.secret}` } }).then((r) => r.json());
    const finance = plans.find((p) => p.name.startsWith('Finance'));
    if (!finance) throw new Error('no finance plan');
    await page.getByRole('combobox', { name: 'Plan', exact: true }).selectOption(finance.id);
    const s = await agree(finance.id, 30);
    const all = await stats();
    if (all && s.total === all.total) throw new Error(`the plan's ${s.total} migrations are all of them: the scope is not visible`);
  });

  if (errors.length) bad('no uncaught errors in the page', errors.slice(0, 3).join(' | '));
  else ok('no uncaught errors in the page');
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
