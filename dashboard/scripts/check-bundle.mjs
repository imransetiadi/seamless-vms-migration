// Fails the build when the start-up bundle pulls in a chunk meant to load with the pages that need it.
// The entry, and every chunk it imports statically, load before the first paint; Recharts (`charts`, ≈ 100 kB
// gzip) belongs only to the pages that draw charts, which import it through their lazy route (SDD §16).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const dist = process.argv[2] ?? 'dist';
const LAZY_ONLY = [/^charts-/];

const html = readFileSync(join(dist, 'index.html'), 'utf8');
const entries = [...html.matchAll(/<script[^>]+type="module"[^>]+src="\/assets\/([^"]+\.js)"/g)].map((m) => m[1]);
if (entries.length === 0) throw new Error(`no module entry script in ${dist}/index.html`);

// static imports only: `import{…}from"./x.js"`, `import"./x.js"`, `export{…}from"./x.js"`; never `import("./x.js")`
const STATIC_IMPORT = /(?:\bimport|\bexport)\s*(?:[\w${},\s*]+?\s*from\s*)?["']\.\/([^"']+\.js)["']/g;
const startup = new Set();
const queue = [...entries];
while (queue.length > 0) {
  const file = queue.pop();
  if (startup.has(file)) continue;
  startup.add(file);
  const code = readFileSync(join(dist, 'assets', file), 'utf8');
  for (const m of code.matchAll(STATIC_IMPORT)) queue.push(m[1]);
}

const bytes = [...startup].reduce((sum, f) => sum + readFileSync(join(dist, 'assets', f)).length, 0);
const misplaced = [...startup].filter((f) => LAZY_ONLY.some((re) => re.test(f)));
if (misplaced.length > 0) {
  console.error(`check-bundle: the start-up bundle loads ${misplaced.join(', ')}, which only chart pages need`);
  console.error(`check-bundle: start-up chunks: ${[...startup].sort().join(', ')}`);
  process.exit(1);
}
console.log(`check-bundle: ${startup.size} start-up chunks, ${(bytes / 1024).toFixed(1)} KiB, none of them lazy-only`);
