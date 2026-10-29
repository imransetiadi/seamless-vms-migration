#!/usr/bin/env node
// demo-stream-browser.mjs - the Events page's live stream across a dropped connection (QASuite UI-06, UI-09; SDD §12,
// §16), on a throwaway demo server of this checkout.
//
//   npm --prefix dashboard run build                 # the server serves dashboard/dist
//   node tests/e2e/demo-stream-browser.mjs           # needs seamless/.venv and the dashboard's Playwright
//
// Creates a throwaway viewer token with `seamless token create` in its own data directory (it is never printed) and
// starts `seamless serve --demo` on 127.0.0.1 with authentication on, a free port and a clean environment; the seeded
// plans run and write events all along. Headless Chromium reaches the server through a TCP relay of this script. The
// viewer opens the Events page: it appends events as they come, with no reload, each once and none missing from its
// window. The relay drops every connection and refuses new ones while the server goes on writing events: the indicator
// reads Reconnecting… while the page is cut off and Live once the relay lets it through again, and the page then lists
// the events written meanwhile, each once. The server restarts on the same data: Reconnecting… while it is down, Live
// once it is back, and every event of the window listed once - those from before the restart and those after it.
// SEAMLESS (the seamless command), PORT (the server's) and SHOT_DIR (a screenshot of the page when a check fails)
// override the defaults. Exit code = number of failed checks.
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { connect, createServer } from 'node:net';
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

/**
 * A TCP relay to the server on `target`: `cut()` drops every connection through it and refuses new ones while the
 * server keeps running - a dropped connection, not a stopped server; `mend()` lets connections through again.
 */
function relay(target) {
  const open = new Set();
  let refusing = false;
  const listener = createServer((client) => {
    if (refusing) {
      client.destroy();
      return;
    }
    const upstream = connect(Number(target), '127.0.0.1');
    for (const [socket, other] of [[client, upstream], [upstream, client]]) {
      open.add(socket);
      socket.on('error', () => other.destroy());
      socket.on('close', () => open.delete(socket));
    }
    client.pipe(upstream);
    upstream.pipe(client);
  });
  const cut = () => {
    refusing = true;
    for (const socket of open) socket.destroy();
  };
  return new Promise((resolve, reject) => {
    listener.on('error', reject);
    listener.listen(0, '127.0.0.1', () => resolve({
      port: listener.address().port,
      cut,
      mend: () => { refusing = false; },
      close: () => new Promise((done) => { cut(); listener.close(() => done()); }),
    }));
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
const data = mkdtempSync(join(tmpdir(), 'seamless-stream-browser.'));
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
  viewer = token('stream-viewer', 'viewer');
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
      SEAMLESS_DEMO_FAILURE_RATE: '0',
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
let link = null;

async function finish(reason) {
  if (reason) console.log(`STOP  ${reason}`);
  await browser?.close();
  await link?.close();
  await stop();
  rmSync(data, { recursive: true, force: true });
  console.log(`== ${pass} passed, ${fail} failed ==`);
  process.exit(fail);
}

/** The newest persisted events the server holds below `before`, newest first: what the page's window should list. */
async function serverTail(before) {
  try {
    const response = await fetch(`${base}/api/v1/events?tail=true&limit=200${before ? `&before=${before}` : ''}`, {
      headers: { Authorization: `Bearer ${viewer.secret}` },
      signal: AbortSignal.timeout(10_000),
    });
    return response.ok ? (await response.json()).map((e) => e.seq) : null;
  } catch {
    return null;
  }
}

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

  // the browser's way to the server: what the relay drops, the server never sees go
  link = await relay(port);
  const site = `http://127.0.0.1:${link.port}`;
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
      if (process.env.SHOT_DIR) await page.screenshot({ path: join(process.env.SHOT_DIR, `demo-stream-${shots}.png`), fullPage: true });
      if (needed) await finish('nothing left to check');
    }
  };
  /** The sequence numbers of the persisted events the Events page lists, in the order shown. */
  const listed = () => page.evaluate(() => [...document.querySelectorAll('main li, main tr')].map((el) => el.textContent?.match(/#(\d+)/)?.[1]).filter(Boolean).map(Number));
  /** What the rail's stream indicator says. */
  const indicator = async () => ((await page.locator('aside, nav').first().textContent().catch(() => '')) ?? '').match(/Live|Reconnecting…|Connecting…|Offline/)?.[0] ?? '(none)';
  /** Each persisted event listed once, newest first, and every one the server holds in the page's window. */
  const wholeWindow = async () => {
    const seqs = await listed();
    if (!seqs.length) throw new Error('the page lists no event');
    if (new Set(seqs).size !== seqs.length) throw new Error(`${seqs.length - new Set(seqs).size} event(s) listed twice`);
    if (seqs.some((s, i) => i > 0 && seqs[i - 1] < s)) throw new Error('the events are not newest first');
    const top = seqs[0];
    const held = ((await serverTail(top + 1)) ?? []).filter((s) => s >= seqs.at(-1));
    const missing = held.filter((s) => !seqs.includes(s));
    if (missing.length) throw new Error(`missing ${missing.slice(0, 10).join(', ')}${missing.length > 10 ? '…' : ''} of the ${held.length} the server holds from #${seqs.at(-1)} to #${top}`);
    return top;
  };

  await check('the viewer signs in and opens the Events page', async () => {
    await page.goto(`${site}/login`);
    await page.getByLabel('API token').fill(viewer.secret);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.getByRole('heading', { level: 1, name: 'Overview' }).waitFor({ timeout: 15_000 });
    await page.goto(`${site}/events`);
    await page.getByRole('heading', { level: 1, name: 'Events' }).waitFor({ timeout: 15_000 });
    if (!(await until(indicator, (s) => s === 'Live', 20))) throw new Error(`the stream reads ${await indicator()}`);
  }, { needed: true });

  await check('the Events page appends live events, with no reload (UI-06)', async () => {
    await sleep(2000);
    const first = (await listed())[0] ?? 0;
    // the seeded plans keep writing: wait for a newer event to reach the page
    const newer = await until(async () => (await listed())[0] ?? 0, (top) => top > first, 30);
    if (!newer) throw new Error(`the newest listed event stayed #${first}`);
    await wholeWindow();
  }, { needed: true });

  let cutAt = 0;
  await check('when the connection drops the stream reads Reconnecting… (UI-09)', async () => {
    link.cut();
    if (!(await until(indicator, (s) => s === 'Reconnecting…', 20))) throw new Error(`it reads ${await indicator()}`);
    cutAt = (await listed())[0] ?? 0;
  }, { needed: true });

  // the server's events after the page's newest, written while nothing reaches the page
  let missed = [];
  await check('the server goes on writing events while the page is cut off', async () => {
    const newer = await until(async () => (await serverTail(0))?.filter((s) => s > cutAt) ?? null, (seqs) => seqs.length >= 2, 30);
    if (!newer) throw new Error(`the server wrote no two events after #${cutAt}`);
    missed = newer;
  }, { needed: true });

  await check('once the connection is back the stream reads Live again (UI-09)', async () => {
    link.mend();
    if (!(await until(indicator, (s) => s === 'Live', 40))) throw new Error(`it reads ${await indicator()}`);
  }, { needed: true });

  await check('the page then lists the events written while it was cut off, each once (UI-09)', async () => {
    const last = Math.max(...missed);
    if (!(await until(async () => (await listed())[0] ?? 0, (top) => top >= last, 30))) throw new Error(`the newest listed event stayed below #${last}`);
    await sleep(2500);
    const seqs = await listed();
    const lost = missed.filter((s) => !seqs.includes(s));
    if (lost.length) throw new Error(`missing ${lost.join(', ')} of the ${missed.length} written from #${Math.min(...missed)} to #${last}`);
    await wholeWindow();
  });

  let before = 0;
  await check('while the server is down the stream reads Reconnecting… (UI-09)', async () => {
    before = (await listed())[0] ?? 0;
    await stop();
    if (!(await until(indicator, (s) => s === 'Reconnecting…', 20))) throw new Error(`it reads ${await indicator()}`);
  }, { needed: true });

  if (await start()) ok('the server restarts on the same data');
  else {
    bad('the server restarts on the same data', lastLog());
    await finish('the demo server did not restart');
  }

  await check('once it is back the stream reads Live again (UI-09)', async () => {
    if (!(await until(indicator, (s) => s === 'Live', 40))) throw new Error(`it reads ${await indicator()}`);
  }, { needed: true });

  await check('after the restart the page lists every event of its window once, old and new (UI-09)', async () => {
    const newer = await until(async () => (await listed())[0] ?? 0, (top) => top > before, 40);
    if (!newer) throw new Error(`no event after #${before} reached the page`);
    await sleep(2500);
    await wholeWindow();
  });

  if (errors.length) bad('no uncaught errors in the page', errors.slice(0, 3).join(' | '));
  else ok('no uncaught errors in the page');
} catch (error) {
  bad('the journey', String(error.message).split('\n')[0]);
}
await finish();
