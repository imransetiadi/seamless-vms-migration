import { createServer } from 'node:http';
import { expect, test } from '@playwright/test';

test('explicit reload recovers a real HTTP page-chunk failure, including repeated failures', async ({ page, baseURL }) => {
  let failures = 2;
  let failed = 0;
  const server = createServer(async (request, response) => {
    try {
      if (/\/assets\/Plans-[\w-]+\.js(?:\?|$)/.test(request.url ?? '') && failures > 0) {
        failures -= 1;
        failed += 1;
        response.writeHead(503, { 'Content-Type': 'text/javascript', 'Cache-Control': 'no-store' });
        response.end('temporarily unavailable');
        return;
      }
      const upstream = await fetch(new URL(request.url ?? '/', baseURL));
      const headers: Record<string, string> = {};
      upstream.headers.forEach((value, name) => { if (!['content-encoding', 'content-length'].includes(name)) headers[name] = value; });
      response.writeHead(upstream.status, headers);
      response.end(Buffer.from(await upstream.arrayBuffer()));
    } catch {
      response.writeHead(502);
      response.end('upstream unavailable');
    }
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('HTTP fixture did not start');
  try {
    await page.goto(`http://127.0.0.1:${address.port}/login`);
    await page.getByLabel('API token', { exact: true }).fill('admin');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Plans', exact: true }).click();
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await expect(page.getByRole('alert')).toContainText('This page could not be loaded');
      await page.getByRole('button', { name: 'Reload page', exact: true }).click();
      await expect.poll(() => failed).toBe(2);
    }
    await expect(page.getByRole('heading', { name: 'Plans', exact: true })).toBeVisible();
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
