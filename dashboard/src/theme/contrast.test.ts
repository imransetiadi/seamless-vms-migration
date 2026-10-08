import { describe, expect, it } from 'vitest';
import css from '../index.css?raw';
import { composite, contrastRatio, parseThemeTokens, toHex, type RGB } from './contrast';

const themes = parseThemeTokens(css);
const THEMES = ['dark', 'light'] as const;

function token(theme: (typeof THEMES)[number], name: string): RGB {
  const value = themes[theme][name];
  if (!value) throw new Error(`token --${name} missing in ${theme} theme`);
  return value;
}

describe('design tokens are exactly SDD §16', () => {
  it('dark theme (default)', () => {
    const expected: Record<string, string> = {
      background: '#0F172A',
      card: '#1B2336',
      muted: '#272F42',
      border: '#475569',
      foreground: '#F8FAFC',
      'muted-foreground': '#94A3B8',
      primary: '#1E293B',
      accent: '#22C55E',
      'accent-foreground': '#0F172A',
      destructive: '#EF4444',
      ring: '#FFFFFF',
    };
    for (const [name, hex] of Object.entries(expected)) expect(toHex(token('dark', name)), name).toBe(hex);
  });

  it('light theme', () => {
    const expected: Record<string, string> = {
      background: '#F8FAFC',
      foreground: '#1E293B',
      card: '#FFFFFF',
      muted: '#E9EFF8',
      'muted-foreground': '#475569',
      border: '#E2E8F0',
      primary: '#2563EB',
      accent: '#EA580C',
      destructive: '#DC2626',
      ring: '#2563EB',
    };
    for (const [name, hex] of Object.entries(expected)) expect(toHex(token('light', name)), name).toBe(hex);
  });

  it('defines the same token set in both themes', () => {
    expect(Object.keys(themes.light).sort()).toEqual(Object.keys(themes.dark).sort());
  });
});

const SURFACES = ['background', 'card', 'muted'];
const STATUS = ['status-neutral', 'status-info', 'status-progress', 'status-warning', 'status-success', 'status-danger'];

describe.each(THEMES)('WCAG 2.2 AA contrast — %s theme', (theme) => {
  const t = (name: string) => token(theme, name);

  it.each([
    ['foreground', 'background'],
    ['foreground', 'card'],
    ['foreground', 'muted'],
    ['card-foreground', 'card'],
    ['muted-foreground', 'background'],
    ['muted-foreground', 'card'],
    ['muted-foreground', 'muted'],
    ['primary-foreground', 'primary'],
    ['accent-foreground', 'accent'],
    ['destructive-foreground', 'destructive'],
  ])('text %s on %s ≥ 4.5:1', (fg, bg) => {
    expect(contrastRatio(t(fg), t(bg))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(STATUS.flatMap((s) => SURFACES.map((surface) => [s, surface])))('status text %s on %s ≥ 4.5:1', (status, surface) => {
    expect(contrastRatio(t(status), t(surface))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(STATUS)('badge text %s on its own 10%% tint (over card and background) ≥ 4.5:1', (status) => {
    for (const surface of ['card', 'background']) {
      const tint = composite(t(status), t(surface), 0.1);
      expect(contrastRatio(t(status), tint), surface).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(['foreground', 'muted-foreground'])('%s on alert tints (danger/warning/success 10%%) ≥ 4.5:1', (fg) => {
    for (const status of ['status-danger', 'status-warning', 'status-success']) {
      for (const surface of ['card', 'background']) {
        expect(contrastRatio(t(fg), composite(t(status), t(surface), 0.1)), `${status} over ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it.each(['background', 'card'])('focus ring on %s ≥ 3:1 (non-text)', (surface) => {
    expect(contrastRatio(t('ring'), t(surface))).toBeGreaterThanOrEqual(3);
  });

  it.each(['background', 'card'])('form control boundary (input) on %s ≥ 3:1', (surface) => {
    expect(contrastRatio(t('input'), t(surface))).toBeGreaterThanOrEqual(3);
  });

  it.each(['chart-1', 'chart-2', 'chart-reference'])('chart mark %s on card and background ≥ 3:1', (mark) => {
    expect(contrastRatio(t(mark), t('card'))).toBeGreaterThanOrEqual(3);
    expect(contrastRatio(t(mark), t('background'))).toBeGreaterThanOrEqual(3);
  });

  it.each(STATUS.filter((s) => s !== 'status-neutral'))('progress fill %s on the muted track ≥ 3:1', (status) => {
    expect(contrastRatio(t(status), t('muted'))).toBeGreaterThanOrEqual(3);
  });

  it('primary action (accent button) stands out from the page ≥ 3:1', () => {
    expect(contrastRatio(t('accent'), t('background'))).toBeGreaterThanOrEqual(3);
  });
});

describe('contrast helpers', () => {
  it('match the WCAG reference values', () => {
    expect(contrastRatio([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 5);
    expect(contrastRatio([255, 255, 255], [255, 255, 255])).toBeCloseTo(1, 5);
    expect(contrastRatio([37, 99, 235], [255, 255, 255])).toBeCloseTo(5.17, 2);
    expect(composite([255, 255, 255], [0, 0, 0], 0.5)).toEqual([128, 128, 128]);
  });
});
