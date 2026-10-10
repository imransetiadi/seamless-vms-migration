/**
 * The brand mark (SDD §16): a slanted S in one unbroken stroke on a rounded tile. Its upper half is
 * the dark theme's foreground and its lower half the accent: a VM leaves its source and lands live on
 * RHOSO without a break. Fixed colours in both themes; `public/favicon.svg` draws the same mark.
 */
export const BRAND_MARK = {
  viewBox: '0 0 32 32',
  /** Two 245° arcs of radius 5.2 meeting at the centre, point-symmetric. */
  upper: 'M20.713 8.602A5.2 5.2 0 1 0 16 16',
  lower: 'M16 16A5.2 5.2 0 1 1 11.287 23.398',
  /** Leans forward 8°, shifted back so the S stays centred on the tile. */
  slant: 'translate(2.25 0) skewX(-8)',
  stroke: 3.6,
  colors: { upper: '#F8FAFC', lower: '#22C55E', tileFrom: '#1E293B', tileTo: '#0F172A', border: '#94A3B8' },
} as const;
