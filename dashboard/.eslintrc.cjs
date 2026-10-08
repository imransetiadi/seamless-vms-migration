/* Lightweight lint config: TypeScript, React hooks rules and jsx-a11y (accessibility). */
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks', 'react-refresh', 'jsx-a11y'],
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
    'plugin:jsx-a11y/recommended',
  ],
  ignorePatterns: ['dist', 'node_modules', 'coverage', 'design-system', 'postcss.config.js'],
  rules: {
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    // Labelled scrollable regions must be focusable for keyboard scrolling (components/ScrollRegion).
    'jsx-a11y/no-noninteractive-tabindex': ['error', { tags: [], roles: ['tabpanel', 'region'] }],
  },
  overrides: [
    {
      files: ['.eslintrc.cjs'],
      env: { node: true },
      rules: { '@typescript-eslint/no-require-imports': 'off' },
    },
    {
      files: ['vite.config.ts', 'tailwind.config.ts'],
      env: { node: true },
    },
    {
      files: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'src/test/**'],
      rules: { 'react-refresh/only-export-components': 'off' },
    },
  ],
};
