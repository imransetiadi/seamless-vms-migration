import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BRAND_MARK } from '../lib/brand';
import { BrandMark } from './BrandMark';

describe('BrandMark (SDD §16)', () => {
  it('draws one slanted S in two halves, foreground then accent, and stays decorative', () => {
    const { container } = render(<BrandMark className="size-7" />);
    const svg = container.querySelector('svg')!;
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveAttribute('viewBox', '0 0 32 32');
    expect(svg).toHaveAttribute('data-brand-mark');
    expect(svg).toHaveClass('size-7');
    const [upper, lower] = Array.from(svg.querySelectorAll('path'));
    expect(upper).toHaveAttribute('d', BRAND_MARK.upper);
    expect(upper).toHaveAttribute('stroke', '#F8FAFC');
    expect(lower).toHaveAttribute('d', BRAND_MARK.lower);
    expect(lower).toHaveAttribute('stroke', '#22C55E');
    expect(upper?.parentElement).toHaveAttribute('transform', BRAND_MARK.slant);
  });

  it('gives each mark its own tile gradient, so a mark beside a hidden one keeps its fill', () => {
    const { container } = render(
      <>
        <BrandMark />
        <BrandMark />
      </>,
    );
    const marks = Array.from(container.querySelectorAll('svg'));
    const ids = marks.map((svg) => svg.querySelector('linearGradient')!.id);
    expect(new Set(ids).size).toBe(2);
    marks.forEach((svg, i) => {
      expect(ids[i]).toMatch(/^[A-Za-z][\w-]*$/);
      expect(svg.querySelector('rect')).toHaveAttribute('fill', `url(#${ids[i]})`);
    });
  });
});
