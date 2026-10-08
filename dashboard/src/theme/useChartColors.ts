import { useMemo } from 'react';
import { tokenColor, useTheme, type Theme } from './context';

export interface ChartColors {
  series1: string;
  series2: string;
  reference: string;
  grid: string;
  axis: string;
  surface: string;
  danger: string;
  foreground: string;
}

/** `theme` is the cache key: the <html> class (and so every token) changes with it. */
function resolveChartColors(_theme: Theme): ChartColors {
  return {
    series1: tokenColor('chart-1'),
    series2: tokenColor('chart-2'),
    reference: tokenColor('chart-reference'),
    grid: tokenColor('chart-grid'),
    axis: tokenColor('muted-foreground'),
    surface: tokenColor('card'),
    danger: tokenColor('status-danger'),
    foreground: tokenColor('foreground'),
  };
}

/** Chart colours resolved from the design tokens of the active theme (Recharts needs literal colours). */
export function useChartColors(): ChartColors {
  const { theme } = useTheme();
  return useMemo(() => resolveChartColors(theme), [theme]);
}

/** Honour prefers-reduced-motion for chart entrance animations. */
export function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}
