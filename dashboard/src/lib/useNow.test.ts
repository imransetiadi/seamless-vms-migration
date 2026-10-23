import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useNow } from './useNow';

describe('useNow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-23T10:00:00Z'));
  });
  afterEach(() => vi.useRealTimers());

  it('ticks every interval while enabled', () => {
    const start = Date.now();
    const { result } = renderHook(() => useNow(1000));
    expect(result.current).toBe(start);
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(start + 1000);
    act(() => vi.advanceTimersByTime(2500));
    expect(result.current).toBe(start + 3000);
  });

  it('stands still while disabled, and reads the time at once when it is enabled again', () => {
    const start = Date.now();
    const { result, rerender } = renderHook(({ on }: { on: boolean }) => useNow(1000, on), { initialProps: { on: false } });
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toBe(start);
    // a clock that starts late shows no stale time: the time is read as it starts, not a tick later
    rerender({ on: true });
    expect(result.current).toBe(start + 5000);
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(start + 6000);
    // stopped again: it keeps the time it read last
    rerender({ on: false });
    act(() => vi.advanceTimersByTime(3000));
    expect(result.current).toBe(start + 6000);
  });

  it('reads the time at once when its interval changes', () => {
    const start = Date.now();
    const { result, rerender } = renderHook(({ every }: { every: number }) => useNow(every), { initialProps: { every: 1000 } });
    act(() => vi.advanceTimersByTime(900));
    expect(result.current).toBe(start);
    rerender({ every: 250 });
    expect(result.current).toBe(start + 900);
    act(() => vi.advanceTimersByTime(250));
    expect(result.current).toBe(start + 1150);
  });
});
