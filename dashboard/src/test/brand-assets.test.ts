import { describe, expect, it } from 'vitest';
import indexHtml from '../../index.html?raw';
import { BRAND_MARK } from '../lib/brand';

// the files the browser loads, as shipped
const favicons = import.meta.glob('../../public/favicon.svg', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
const pngs = import.meta.glob('../../public/*.png', { query: '?inline', import: 'default', eager: true }) as Record<string, string>;

describe('the brand assets draw the brand mark (SDD §16)', () => {
  it('the favicon draws the same S as BrandMark', () => {
    const favicon = favicons['../../public/favicon.svg'];
    for (const part of [BRAND_MARK.upper, BRAND_MARK.lower, BRAND_MARK.slant, '#F8FAFC', '#22C55E', '#1E293B', '#0F172A']) {
      expect(favicon).toContain(part);
    }
  });

  it('index.html links the SVG favicon and the 180 px apple-touch icon', () => {
    expect(indexHtml).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
    expect(indexHtml).toContain('<link rel="apple-touch-icon" href="/apple-touch-icon.png" />');
  });

  it('the apple-touch icon is a 180 × 180 PNG', () => {
    const url = pngs['../../public/apple-touch-icon.png'] ?? '';
    expect(url).toMatch(/^data:image\/png;base64,/);
    const bytes = Uint8Array.from(atob(url.split(',')[1] ?? ''), (c) => c.charCodeAt(0));
    expect(String.fromCharCode(...bytes.slice(1, 4))).toBe('PNG');
    const view = new DataView(bytes.buffer);
    expect([view.getUint32(16), view.getUint32(20)]).toEqual([180, 180]);
  });
});
