import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { applyTheme, readStoredTheme, THEME_STORAGE_KEY, ThemeContext, type Theme } from './context';

export function ThemeProvider({ children, initialTheme }: { children: ReactNode; initialTheme?: Theme }) {
  // The <html> class is updated synchronously (before React re-renders), so components that
  // resolve colour tokens during render — charts — always read the new theme's values.
  const [theme, setThemeState] = useState<Theme>(() => {
    const initial = initialTheme ?? readStoredTheme();
    applyTheme(initial);
    return initial;
  });

  const setTheme = useCallback((next: Theme) => {
    applyTheme(next);
    setThemeState(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // preference not persisted — fine
    }
  }, []);

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme: () => setTheme(theme === 'dark' ? 'light' : 'dark') }),
    [theme, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
