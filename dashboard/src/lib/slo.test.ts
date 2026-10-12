import { describe, expect, it } from 'vitest';
import { barLabelText, sloTone } from './slo';

describe('sloTone', () => {
  it('is calm below 80 % of the SLO, a warning from 80 % and a breach past it (SDD §16)', () => {
    expect(sloTone(239, 300)).toBe('success'); // 79.7 %
    expect(sloTone(240, 300)).toBe('warning'); // 80 %
    expect(sloTone(300, 300)).toBe('warning'); // at the SLO
    expect(sloTone(301, 300)).toBe('danger');
  });
});

// Recharts' Text splits a label into words at these (recharts 2.x, Text.js BREAKING_SPACES) and wraps
// them into the label's width - for a bar's right-hand label, the width of the bar itself
const BREAKING_SPACES = /[ \f\n\r\t\v\u2028\u2029]/;

describe('the label beside a Downtime vs SLO bar (SDD §16)', () => {
  it('stays in one piece however short its bar: "4 s", never "4" above "s"', () => {
    for (const [downtime, over] of [[4, false], [8.3, false], [75, true], [3725, true]] as const) {
      const text = barLabelText(downtime, over);
      expect(text, text).not.toMatch(BREAKING_SPACES);
    }
    expect(barLabelText(4, false)).toBe('4\u00a0s');
    expect(barLabelText(75, true)).toBe('1m\u00a015s\u00a0·\u00a0over\u00a0SLO');
  });
});
