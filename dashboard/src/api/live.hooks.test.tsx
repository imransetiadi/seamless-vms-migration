import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { LiveContext, LiveEventHub, useLiveEvents } from './live';
import type { Event } from './types';

function event(kind: string, seq = 0): Event {
  return { seq, ts: '2026-10-08T12:00:01Z', kind, plan_id: null, migration_id: null, actor: 'orchestrator', message: '', data: {} };
}

function withHub(hub: LiveEventHub) {
  return ({ children }: { children: ReactNode }) => <LiveContext.Provider value={hub}>{children}</LiveContext.Provider>;
}

describe('useLiveEvents', () => {
  it("calls the last render's listener for each streamed event, on one subscription", () => {
    const hub = new LiveEventHub();
    const subscribe = vi.spyOn(hub, 'subscribe');
    const heard: string[] = [];
    const { rerender } = renderHook(({ page }: { page: string }) => useLiveEvents((e) => heard.push(`${page} ${e.kind}`)), {
      wrapper: withHub(hub),
      initialProps: { page: 'first' },
    });
    act(() => hub.publish(event('migration.phase', 1)));
    // drawn again with a new listener: the next event goes to it, without subscribing again
    rerender({ page: 'second' });
    act(() => hub.publish(event('migration.action', 2)));
    expect(heard).toEqual(['first migration.phase', 'second migration.action']);
    expect(subscribe).toHaveBeenCalledTimes(1);
  });

  it('hears nothing once unmounted', () => {
    const hub = new LiveEventHub();
    const listener = vi.fn();
    const { unmount } = renderHook(() => useLiveEvents(listener), { wrapper: withHub(hub) });
    act(() => hub.publish(event('migration.phase', 1)));
    unmount();
    act(() => hub.publish(event('migration.phase', 2)));
    expect(listener).toHaveBeenCalledTimes(1);
  });
});
