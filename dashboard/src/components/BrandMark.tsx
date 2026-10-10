import { useId } from 'react';
import { BRAND_MARK } from '../lib/brand';
import { cn } from '../lib/cn';

/**
 * The brand mark (SDD §16), decorative: the product name is the text beside it. Size it with
 * `className` (`size-7` in the sidebar, `size-9` on the sign-in card).
 */
export function BrandMark({ className }: { className?: string }) {
  // the sidebar and the mobile header each hold a mark, one of them hidden: a gradient shared by id
  // would be defined inside the hidden one, and a gradient there does not paint
  const tile = `brand-tile-${useId().replace(/[^\w-]/g, '')}`;
  const { colors } = BRAND_MARK;
  return (
    <svg viewBox={BRAND_MARK.viewBox} aria-hidden data-brand-mark="" className={cn('shrink-0', className)}>
      <defs>
        <linearGradient id={tile} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={colors.tileFrom} />
          <stop offset="1" stopColor={colors.tileTo} />
        </linearGradient>
      </defs>
      <rect x=".5" y=".5" width="31" height="31" rx="8" fill={`url(#${tile})`} stroke={colors.border} strokeOpacity=".3" />
      <g transform={BRAND_MARK.slant} fill="none" strokeWidth={BRAND_MARK.stroke} strokeLinecap="round">
        <path d={BRAND_MARK.upper} stroke={colors.upper} />
        <path d={BRAND_MARK.lower} stroke={colors.lower} />
      </g>
    </svg>
  );
}
