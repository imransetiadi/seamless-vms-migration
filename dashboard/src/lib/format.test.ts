import { describe, expect, it } from 'vitest';
import {
  formatBytes,
  formatClock,
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPct,
  formatRate,
  formatRelative,
} from './format';

describe('formatBytes (IEC units)', () => {
  it('renders an em dash for missing or non-finite values', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(undefined)).toBe('—');
    expect(formatBytes(Number.NaN)).toBe('—');
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('keeps plain bytes below 1 KiB as integers', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1)).toBe('1 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('switches unit exactly at powers of 1024', () => {
    expect(formatBytes(1024)).toBe('1.0 KiB');
    expect(formatBytes(1536)).toBe('1.5 KiB');
    expect(formatBytes(1024 ** 2)).toBe('1.0 MiB');
    expect(formatBytes(1024 ** 3)).toBe('1.0 GiB');
    expect(formatBytes(1024 ** 4)).toBe('1.0 TiB');
    expect(formatBytes(1024 ** 5)).toBe('1.0 PiB');
  });

  it('drops decimals at three integer digits and never shows 1024 of a unit', () => {
    expect(formatBytes(500 * 1024 ** 3)).toBe('500 GiB');
    expect(formatBytes(42.46 * 1024 ** 3)).toBe('42.5 GiB');
    // 1023.999 KiB must roll over to the next unit instead of printing "1024 KiB".
    expect(formatBytes(1024 ** 2 - 1)).toBe('1.0 MiB');
  });

  it('caps at PiB and preserves the sign', () => {
    expect(formatBytes(2048 * 1024 ** 5)).toBe('2048 PiB');
    expect(formatBytes(-1536)).toBe('-1.5 KiB');
  });

  it('formats rates as bytes per second', () => {
    expect(formatRate(131072000)).toBe('125 MiB/s');
    expect(formatRate(null)).toBe('—');
  });
});

describe('formatDuration', () => {
  it('renders an em dash for missing values and clamps negatives to zero', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(undefined)).toBe('—');
    expect(formatDuration(Number.NaN)).toBe('—');
    expect(formatDuration(-5)).toBe('0 s');
  });

  it('shows tenths below ten seconds and whole seconds below a minute', () => {
    expect(formatDuration(0)).toBe('0 s');
    expect(formatDuration(0.44)).toBe('0.4 s');
    expect(formatDuration(5)).toBe('5 s');
    expect(formatDuration(9.96)).toBe('10 s');
    expect(formatDuration(45.4)).toBe('45 s');
  });

  it('rounds up into the next unit instead of printing 60 seconds', () => {
    expect(formatDuration(59.6)).toBe('1m 00s');
    expect(formatDuration(60)).toBe('1m 00s');
    expect(formatDuration(65)).toBe('1m 05s');
    expect(formatDuration(3599)).toBe('59m 59s');
  });

  it('uses hours and days for long operations', () => {
    expect(formatDuration(3600)).toBe('1h 00m');
    expect(formatDuration(3661)).toBe('1h 01m');
    expect(formatDuration(86_399)).toBe('23h 59m');
    expect(formatDuration(86_400)).toBe('1d 0h');
    expect(formatDuration(90_061)).toBe('1d 1h');
  });

  it('formats a running clock as M:SS / H:MM:SS', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(95)).toBe('1:35');
    expect(formatClock(3723)).toBe('1:02:03');
    expect(formatClock(-3)).toBe('0:00');
    expect(formatClock(null)).toBe('—');
  });
});

describe('formatPct', () => {
  it('renders an em dash for missing values', () => {
    expect(formatPct(null)).toBe('—');
    expect(formatPct(Number.NaN)).toBe('—');
  });

  it('strips a trailing .0 and keeps one decimal otherwise', () => {
    expect(formatPct(0)).toBe('0%');
    expect(formatPct(42)).toBe('42%');
    expect(formatPct(42.46)).toBe('42.5%');
    expect(formatPct(100)).toBe('100%');
  });

  it('never claims 100% or 0% for values that are not', () => {
    expect(formatPct(99.96)).toBe('99.9%');
    expect(formatPct(0.04)).toBe('<0.1%');
  });

  it('clamps out-of-range values', () => {
    expect(formatPct(-3)).toBe('0%');
    expect(formatPct(140)).toBe('100%');
  });
});

describe('formatRelative', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');

  it('handles missing and invalid timestamps', () => {
    expect(formatRelative(null, now)).toBe('—');
    expect(formatRelative('not-a-date', now)).toBe('—');
  });

  it('describes past instants', () => {
    expect(formatRelative('2026-10-08T11:59:58Z', now)).toBe('just now');
    expect(formatRelative('2026-10-08T11:59:18Z', now)).toBe('42 s ago');
    expect(formatRelative('2026-10-08T11:55:00Z', now)).toBe('5 min ago');
    expect(formatRelative('2026-10-08T09:00:00Z', now)).toBe('3 h ago');
    expect(formatRelative('2026-10-06T12:00:00Z', now)).toBe('2 d ago');
  });

  it('describes future instants', () => {
    expect(formatRelative('2026-10-08T12:05:00Z', now)).toBe('in 5 min');
  });
});

describe('formatDateTime / formatNumber', () => {
  it('renders ISO timestamps as local wall-clock time', () => {
    expect(formatDateTime('2026-10-08T07:05:09Z')).toBe('2026-10-08 07:05:09');
    expect(formatDateTime(null)).toBe('—');
  });

  it('groups thousands', () => {
    expect(formatNumber(1234567)).toBe('1,234,567');
    expect(formatNumber(null)).toBe('—');
  });
});
