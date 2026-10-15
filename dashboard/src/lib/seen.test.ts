import { describe, expect, it } from 'vitest';
import { createSeenSeqs } from './seen';

describe('createSeenSeqs (perf audit PF-07)', () => {
  it('says whether a sequence number is new, so a replayed event is shown once', () => {
    const seen = createSeenSeqs(10);
    expect(seen.add(5)).toBe(true);
    expect(seen.add(5)).toBe(false);
    expect(seen.add(6)).toBe(true);
    // within the window an earlier number that never came is still new
    expect(seen.add(4)).toBe(true);
    expect(seen.add(4)).toBe(false);
  });

  it('keeps at most its bound, the oldest going first; anything at or below the highest gone counts as seen', () => {
    const seen = createSeenSeqs(100);
    for (let seq = 1; seq <= 150; seq++) expect(seen.add(seq)).toBe(true);
    expect(seen.size).toBe(100);
    // 1..50 went: a replay of one of them is still not shown again
    expect(seen.add(10)).toBe(false);
    expect(seen.add(50)).toBe(false);
    // those kept are still known, and a new one is new
    expect(seen.add(51)).toBe(false);
    expect(seen.add(151)).toBe(true);
    expect(seen.size).toBe(100);
  });
});
