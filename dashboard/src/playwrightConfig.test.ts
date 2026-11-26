import { afterEach, describe, expect, it, vi } from 'vitest';

// The browser smoke was the CI critical path (186 tests, 8.4 min on Playwright's default of half the
// runner's 4 vCPUs); a static `vite preview` serves 4 browsers easily. The config uses Node APIs, so
// this file is typechecked with it in tsconfig.e2e.json and excluded from the app's tsconfig.json.
describe('playwright.config.ts', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('runs four workers on CI', async () => {
    vi.stubEnv('CI', 'true');
    const { default: config } = await import('../playwright.config');
    expect(config.workers).toBe(4);
  });

  it("keeps Playwright's own default locally", async () => {
    vi.stubEnv('CI', '');
    const { default: config } = await import('../playwright.config');
    expect(config.workers).toBeUndefined();
  });
});
