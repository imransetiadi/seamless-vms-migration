import { defineConfig, devices } from '@playwright/test';

// Browser smoke of the built dashboard in mock mode (QASuite §12 "Playwright (optional)"):
// `vite build` with VITE_SEAMLESS_MOCK=1 serves fixture data in the browser, so the test needs
// no control plane. It is the one check that exercises the real CSS pipeline (Tailwind 4).
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'VITE_SEAMLESS_MOCK=1 npx vite build --outDir dist-mock && npx vite preview --outDir dist-mock --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/login',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
