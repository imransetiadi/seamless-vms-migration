/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" switches the dashboard to the in-browser mock adapter (src/api/mock.ts). */
  readonly VITE_SEAMLESS_MOCK?: string;
  /** Override the API base path (default `/api/v1`). */
  readonly VITE_SEAMLESS_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
