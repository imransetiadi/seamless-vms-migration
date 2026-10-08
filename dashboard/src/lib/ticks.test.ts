import { describe, expect, it } from 'vitest';
import { formatDurationTick, niceByteTicks, niceDurationTicks, niceStep, niceTicks } from './ticks';

const MiB = 1024 ** 2;

describe('nice axis ticks', () => {
  it('picks 1/2/2.5/5/10 steps', () => {
    expect(niceStep(28.5)).toBe(50);
    expect(niceStep(0.3)).toBe(0.5);
    expect(niceStep(7)).toBe(10);
    expect(niceTicks(114)).toEqual([0, 50, 100, 150]);
  });

  it('keeps byte ticks round in the display unit', () => {
    expect(niceByteTicks(114 * MiB)).toEqual([0, 50 * MiB, 100 * MiB, 150 * MiB]);
  });

  it('uses human duration steps', () => {
    expect(niceDurationTicks(923)).toEqual([0, 300, 600, 900, 1200]);
    expect(formatDurationTick(300)).toBe('5 min');
    expect(formatDurationTick(330)).toBe('5m 30s');
    expect(formatDurationTick(5400)).toBe('1h 30m');
    expect(formatDurationTick(45)).toBe('45 s');
  });

  it('handles an all-zero series', () => {
    expect(niceTicks(0)).toEqual([0, 1]);
  });
});
