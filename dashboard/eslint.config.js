/* Lightweight lint config: TypeScript, React hooks rules and jsx-a11y (accessibility). */
import js from '@eslint/js';
import tsPlugin from '@typescript-eslint/eslint-plugin';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';

// The sources ESLint 8 linted with `--ext .ts,.tsx`; the shared configs below apply to them alone.
const sources = ['**/*.{ts,tsx}'];

export default [
  { ignores: ['dist', 'node_modules', 'coverage', 'design-system', 'postcss.config.js'] },
  ...[
    js.configs.recommended,
    ...tsPlugin.configs['flat/recommended'],
    reactHooks.configs['recommended-latest'],
    jsxA11y.flatConfigs.recommended,
  ].map((config) => ({ ...config, files: config.files ?? sources })),
  {
    files: sources,
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { 'react-refresh': reactRefresh },
    rules: {
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // Labelled scrollable regions must be focusable for keyboard scrolling (components/ScrollRegion).
      'jsx-a11y/no-noninteractive-tabindex': ['error', { tags: [], roles: ['tabpanel', 'region'] }],
    },
  },
  {
    files: ['vite.config.ts'],
    languageOptions: { globals: globals.node },
  },
  {
    files: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'src/test/**'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
];
