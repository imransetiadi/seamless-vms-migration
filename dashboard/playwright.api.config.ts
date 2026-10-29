import { defineConfig, devices } from '@playwright/test';

// Real fetch paths with synthetic API responses; the in-browser demo bypasses network interception.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'api-audit.spec.ts',
  timeout: 30_000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 2,
  use: { baseURL: 'http://127.0.0.1:4183', trace: 'retain-on-failure', reducedMotion: 'reduce' },
  webServer: {
    command: 'npx vite build --outDir dist/api-audit && npx vite preview --outDir dist/api-audit --host 127.0.0.1 --port 4183 --strictPort',
    url: 'http://127.0.0.1:4183/login',
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
