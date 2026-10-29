import { afterEach, expect, it, vi } from 'vitest';
import { routeChunk } from './routeChunk';

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); history.replaceState(null, '', '/'); });

it('keeps the normal lazy import when no recovery was requested', async () => {
  expect(await routeChunk('Plans', async () => 'page')).toBe('page');
});

it('rejects a manifest asset outside the emitted same-origin asset directory', async () => {
  vi.stubEnv('DEV', false);
  history.replaceState(null, '', '/plans?__seamless_retry=one');
  vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ Plans: 'https://outside.test/page.js' })));
  await expect(routeChunk('Plans', async () => 'page')).rejects.toThrow('The page asset could not be found.');
});

it('reports a failed recovery manifest request without loading an unchecked asset', async () => {
  vi.stubEnv('DEV', false);
  history.replaceState(null, '', '/plans?__seamless_retry=two');
  vi.stubGlobal('fetch', async () => new Response('unavailable', { status: 503 }));
  await expect(routeChunk('Plans', async () => 'page')).rejects.toThrow('The page manifest could not be loaded.');
});
