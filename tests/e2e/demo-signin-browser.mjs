#!/usr/bin/env node
// demo-signin-browser.mjs - signing in and out from the dashboard (SDD §13.1, §16; QASuite UI-01, UI-08), on a
// throwaway demo server of this checkout.
//
//   npm --prefix dashboard run build                 # the server serves dashboard/dist
//   node tests/e2e/demo-signin-browser.mjs           # needs seamless/.venv and the dashboard's Playwright
//
// Creates two throwaway tokens - a viewer and an admin - with `seamless token create` in its own data directory
// (the tokens are never printed), and starts `seamless serve --demo` on 127.0.0.1 with authentication on, a free
// port and a clean environment. In headless Chromium: a deep link while signed out asks to sign in, and Sign in
// offers no way past it; an empty and a wrong token are refused with a reason, the token field keeping the focus;
// the viewer signs in, lands where the deep link pointed and finds Add provider unavailable, with the reason;
// signing out leaves no token in the tab; the admin signs in and lands on Overview. The server restarts without
// the admin's token - it reads the tokens file at startup, so that is how a token is revoked - and the next page
// says the session ended. Last, the server restarts with authentication off, as a loopback demo runs by default:
// Sign in offers to continue without a token, and the link leads into the app. SEAMLESS (the seamless command),
// PORT and SHOT_DIR (a screenshot of the page when a check fails) override the defaults.
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

// another server on the port would be signed in to instead
if ((await unauthenticated('/health')) !== null) {
  console.log(`STOP  something already answers on 127.0.0.1:${port}: pick a free PORT`);
  process.exit(1);
}
// none of the caller's settings reach the server: SQLite and the tokens in its own directory
const data = mkdtempSync(join(tmpdir(), 'seamless-signin-browser.'));
const env = { PATH: process.env.PATH, HOME: process.env.HOME };
if (process.env.PYTHONPATH) env.PYTHONPATH = process.env.PYTHONPATH;

/** A throwaway token: the first line `seamless token create` prints, its YAML entry the rest. */
function token(name, role) {
  const made = spawnSync(seamless, ['token', 'create', '--name', name, '--role', role], { env, encoding: 'utf8' });
  if (made.status !== 0) throw new Error(`seamless token create failed: ${made.stderr.trim().split('\n').pop()}`);
  const [secret, , ...entry] = made.stdout.trim().split('\n');
  return { secret: secret.trim(), entry: entry.join('\n') };
}
let viewer;
let admin;
try {
  viewer = token('signin-viewer', 'viewer');
  admin = token('signin-admin', 'admin');
} catch (error) {
  rmSync(data, { recursive: true, force: true });
  console.log(`STOP  ${error.message}`);
  process.exit(1);
}
const tokensFile = join(data, 'tokens.yaml');
const writeTokens = (...tokens) => writeFileSync(tokensFile, `tokens:\n${tokens.map((t) => t.entry).join('\n')}\n`, { mode: 0o600 });
const lastLog = () => readFileSync(join(data, 'server.log'), 'utf8').split('\n').slice(-5).join(' | ');

let server = null;
let exited = true;
/** Starts the demo server on the same data directory and port: its health once it answers, null if it did not. */
async function start({ auth }) {
  const log = openSync(join(data, 'server.log'), 'a');
  exited = false;
  server = spawn(seamless, ['serve', '--demo', '--host', '127.0.0.1', '--port', port], {
    env: {
      ...env,
      SEAMLESS_DATA_DIR: data,
      SEAMLESS_DEMO_FAILURE_RATE: '0',
      SEAMLESS_DEMO_SPEED: '60',
      // without these a loopback demo runs with authentication off (SDD §13.1)
      ...(auth ? { SEAMLESS_TOKENS_FILE: tokensFile, SEAMLESS_AUTH_DISABLED: 'false' } : {}),
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

try {
  writeTokens(viewer, admin);
  if (await start({ auth: true })) ok(`throwaway demo server healthy on 127.0.0.1:${port}`);
  else {
    bad('throwaway demo server healthy', lastLog());
    await finish('the demo server did not start');
  }
  if ((await unauthenticated('/plans')) === 401) ok('authentication is on: a request without a token is refused');
  else {
    bad('authentication is on');
    await finish('signing in needs authentication on');
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
      if (process.env.SHOT_DIR) await page.screenshot({ path: join(process.env.SHOT_DIR, `demo-signin-${shots}.png`), fullPage: true });
      if (needed) await finish('nothing left to check');
    }
  };
  const signInPage = () => page.getByRole('heading', { level: 1, name: 'Sign in' }).waitFor({ timeout: 20_000 });
  const withoutToken = () => page.getByRole('link', { name: 'continue without a token' });
  const focused = () => page.evaluate(() => document.activeElement?.getAttribute('name'));
  const signIn = async (secret) => {
    await page.getByLabel('API token').fill(secret);
    await page.getByRole('button', { name: 'Sign in' }).click();
  };

  await check('a deep link while signed out asks to sign in', async () => {
    await page.goto(`${base}/providers`);
    await signInPage();
    if (new URL(page.url()).pathname !== '/login') throw new Error(`it shows ${page.url()}`);
  }, { needed: true });

  await check('with authentication on, Sign in offers no way past it (SDD §16)', async () => {
    // once Sign in knows the server: a link past it would lead straight back here
    await page.getByText(/^Control plane .* reachable/).waitFor({ timeout: 10_000 });
    if (await withoutToken().count()) throw new Error("Sign in offers 'continue without a token'");
  });

  await check('an empty token is refused with a reason, the token field keeping the focus', async () => {
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.getByRole('alert').filter({ hasText: 'Paste your API token to sign in.' }).waitFor({ timeout: 5_000 });
    if ((await focused()) !== 'token') throw new Error(`the focus is on ${await focused()}`);
  });

  await check('a wrong token is refused with a reason, the focus back in the token field', async () => {
    await signIn('not-a-valid-token');
    await page.getByRole('alert').filter({ hasText: 'The token was rejected. Check that it is complete and has not been revoked.' }).waitFor({ timeout: 10_000 });
    if ((await focused()) !== 'token') throw new Error(`the focus is on ${await focused()}`);
  });

  await check('the viewer signs in and lands where the deep link pointed (QASuite UI-01)', async () => {
    await signIn(viewer.secret);
    await page.getByRole('heading', { level: 1, name: 'Providers' }).waitFor({ timeout: 15_000 });
    if (new URL(page.url()).pathname !== '/providers') throw new Error(`it shows ${page.url()}`);
  }, { needed: true });

  await check('the rail names the viewer and the role, and Add provider is unavailable with the reason (SDD §13.2)', async () => {
    const who = page.getByText('signin-viewer', { exact: true }).first();
    await who.waitFor({ timeout: 10_000 });
    const named = (await who.locator('xpath=..').textContent()) ?? '';
    if (named !== 'signin-viewerViewer') throw new Error(`the rail says ${JSON.stringify(named)}`);
    const add = page.getByRole('button', { name: 'Add provider' }).first();
    if ((await add.getAttribute('aria-disabled')) !== 'true') throw new Error('Add provider is available to a viewer');
    const why = await add.evaluate((el) => (el.getAttribute('aria-describedby') ?? '').split(' ').map((id) => document.getElementById(id)?.textContent ?? '').join(' '));
    if (!why.includes('Adding a provider requires the admin role.')) throw new Error(`it says ${JSON.stringify(why)}`);
  });

  await check('signing out goes to Sign in, leaves no token in the tab and reports no ended session', async () => {
    await page.getByRole('button', { name: 'Sign out' }).first().click();
    await signInPage();
    const stored = await page.evaluate(() => sessionStorage.getItem('seamless.token'));
    if (new URL(page.url()).pathname !== '/login' || stored !== null) throw new Error(`it shows ${page.url()} and the tab ${stored === null ? 'holds no token' : 'still holds the token'}`);
    await page.goto(`${base}/plans`);
    await signInPage();
    if (await page.getByRole('status', { name: 'Session ended' }).count()) throw new Error('signing out is reported as a session the server ended');
  });

  await check('the admin opens the dashboard, signs in and lands on Overview (QASuite UI-01)', async () => {
    // the dashboard's address, not /login: going to the address it shows is a reload, which keeps the page that
    // asked to sign in (/plans above) for Sign in to return to
    await page.goto(`${base}/`);
    await signInPage();
    await signIn(admin.secret);
    await page.getByRole('heading', { level: 1, name: 'Overview' }).waitFor({ timeout: 15_000 });
  }, { needed: true });

  // the server reads the tokens file at startup: restarting it without the admin's token revokes that token
  await stop();
  writeTokens(viewer);
  if (!(await start({ auth: true }))) {
    bad('the server restarts without the admin token', lastLog());
    await finish('the demo server did not restart');
  }
  await check('a token revoked mid-session leads to Sign in, which says the session ended (QASuite UI-08, SDD §16)', async () => {
    await page.getByRole('link', { name: 'Providers' }).first().click();
    await signInPage();
    const note = page.getByRole('status', { name: 'Session ended' });
    await note.waitFor({ timeout: 5_000 });
    const said = (await note.textContent()) ?? '';
    if (!said.includes('Your session ended: the server no longer accepts your token.')) throw new Error(`it says ${JSON.stringify(said)}`);
    // a note shown on load is not reliably announced: the token field says it too
    const described = await page.getByLabel('API token').evaluate((el) => (el.getAttribute('aria-describedby') ?? '').split(' ').map((id) => document.getElementById(id)?.textContent ?? '').join(' '));
    if (!described.includes('Your session ended')) throw new Error('the token field does not carry the note');
  });

  await stop();
  if ((await start({ auth: false })) && (await unauthenticated('/plans')) === 200) {
    ok('the server restarts with authentication off, as a loopback demo runs by default');
  } else {
    bad('the server restarts with authentication off', lastLog());
    await finish('the demo server did not restart');
  }
  await check('with authentication off, Sign in offers to continue without a token, back to the page that asked (SDD §16)', async () => {
    // a reload asks the server again and keeps the page that asked to sign in: Providers, the revoked session's click
    await page.reload();
    const link = withoutToken();
    await link.waitFor({ timeout: 10_000 });
    const said = (await link.locator('xpath=..').textContent()) ?? '';
    if (said !== 'Authentication is off on this server: continue without a token.') throw new Error(`it says ${JSON.stringify(said)}`);
    await link.click();
    await page.getByRole('heading', { level: 1, name: 'Providers' }).waitFor({ timeout: 15_000 });
    await page.getByText('anonymous', { exact: true }).first().waitFor({ timeout: 10_000 });
  });

  if (errors.length) bad('no uncaught errors in the page', errors.slice(0, 3).join(' | '));
  else ok('no uncaught errors in the page');
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
