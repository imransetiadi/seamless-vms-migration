/** WCAG 2.x contrast maths for the design tokens declared in src/index.css. */

export type RGB = [number, number, number];

export interface ThemeTokens {
  dark: Record<string, RGB>;
  light: Record<string, RGB>;
}

function parseBlock(block: string): Record<string, RGB> {
  const tokens: Record<string, RGB> = {};
  const declaration = /--([a-z0-9-]+):\s*(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s*;/g;
  for (const match of block.matchAll(declaration)) {
    const [, name, r, g, b] = match;
    if (name) tokens[name] = [Number(r), Number(g), Number(b)];
  }
  return tokens;
}

/** Reads `:root, :root.dark { … }` and `:root.light { … }` channel triplets from the stylesheet. */
export function parseThemeTokens(css: string): ThemeTokens {
  const dark = /:root,\s*:root\.dark\s*\{([^}]*)\}/.exec(css)?.[1] ?? '';
  const light = /:root\.light\s*\{([^}]*)\}/.exec(css)?.[1] ?? '';
  return { dark: parseBlock(dark), light: parseBlock(light) };
}

function channel(value: number): number {
  const s = value / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance([r, g, b]: RGB): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a: RGB, b: RGB): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** `fg` at `alpha` over an opaque `bg` (what `bg-status-x/10` renders). */
export function composite(fg: RGB, bg: RGB, alpha: number): RGB {
  return fg.map((c, i) => Math.round(c * alpha + (bg[i] ?? 0) * (1 - alpha))) as RGB;
}

export function toHex(rgb: RGB): string {
  return `#${rgb.map((c) => c.toString(16).padStart(2, '0')).join('').toUpperCase()}`;
}
