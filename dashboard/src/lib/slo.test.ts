import { describe, expect, it } from 'vitest';
import { sloTone } from './slo';

describe('sloTone', () => {
  it('is calm below 80 % of the SLO, a warning from 80 % and a breach past it (SDD §16)', () => {
    expect(sloTone(239, 300)).toBe('success'); // 79.7 %
    expect(sloTone(240, 300)).toBe('warning'); // 80 %
    expect(sloTone(300, 300)).toBe('warning'); // at the SLO
    expect(sloTone(301, 300)).toBe('danger');
  });
});
