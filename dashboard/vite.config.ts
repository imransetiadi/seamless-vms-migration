import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// SDD §16: `npm run dev` proxies /api to the control plane; build output is dashboard/dist.
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8080',
    changeOrigin: false,
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: apiProxy,
    // the dashboard's own files, plus the guest OS case table it shares with the control-plane
    // tests (read by src/lib/guestOs.test.ts only; nothing outside dashboard/ is bundled)
    fs: { allow: ['.', '../seamless/tests/fixtures'] },
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
  build: {
    outDir: 'dist',
    target: 'es2022',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Framework code in `vendor` (needed at start); Recharts and its d3/lodash dependencies in
        // `charts`, loaded only with the pages that draw charts. Pinning React explicitly keeps
        // Rollup from hoisting it into the chart chunk (which would make the entry preload charts).
        manualChunks(id) {
          if (/node_modules\/(react|react-dom|scheduler|react-router|react-router-dom|@remix-run|@tanstack)\//.test(id)) {
            return 'vendor';
          }
          if (/node_modules\/(recharts|recharts-scale|react-smooth|react-is|react-transition-group|dom-helpers|victory-vendor|d3-[^/]+|internmap|lodash|decimal\.js-light|eventemitter3|clsx|tiny-invariant|fast-equals)\//.test(id)) {
            return 'charts';
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // CSS is not needed by component tests, except the token source read by contrast.test.ts.
    css: { include: [/src\/index\.css/] },
    restoreMocks: true,
    // jsdom integration tests (mock API + SSE) need headroom on a busy machine.
    testTimeout: 15_000,
    // Deterministic wall-clock formatting in tests.
    env: { TZ: 'UTC' },
  },
});
