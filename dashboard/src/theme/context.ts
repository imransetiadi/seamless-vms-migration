import { createContext, useContext } from 'react';

export type Theme = 'dark' | 'light';

export const THEME_STORAGE_KEY = 'seamless.theme';

export interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

export const ThemeContext = createContext<ThemeContextValue>({
  theme: 'dark',
  setTheme: () => undefined,
  toggleTheme: () => undefined,
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

/** Dark is the default (SDD §16); an explicit choice is remembered per browser. */
export function readStoredTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.classList.toggle('dark', theme === 'dark');
  root.classList.toggle('light', theme === 'light');
}

/** Resolves a colour token (e.g. `chart-1`) to `rgb(r g b)` for SVG/canvas consumers such as Recharts. */
export function tokenColor(name: string, alpha?: number): string {
  let channels = '';
  try {
    channels = getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
  } catch {
    channels = '';
  }
  if (!channels) channels = '148 163 184';
  return alpha === undefined ? `rgb(${channels})` : `rgb(${channels} / ${alpha})`;
}
