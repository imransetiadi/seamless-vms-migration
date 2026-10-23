import { useCallback, useState, useSyncExternalStore } from 'react';

/** The time as a store: the time it read last, read while rendering. */
interface Clock {
  read(): number;
  /** Ticks every `intervalMs`, reading the time at once - a clock that starts late shows no stale time; returns how to stop. */
  run(intervalMs: number, changed: () => void): () => void;
}

function createClock(): Clock {
  let now = Date.now();
  return {
    read: () => now,
    run(intervalMs, changed) {
      const tick = () => {
        now = Date.now();
        changed();
      };
      tick();
      const id = setInterval(tick, intervalMs);
      return () => clearInterval(id);
    },
  };
}

/**
 * Current time in ms, refreshed every `intervalMs` while `enabled` (for running clocks). The time is outside React, so it
 * is read as an external store (useSyncExternalStore): read again as the clock starts and on each tick, and kept while
 * the clock is stopped.
 */
export function useNow(intervalMs = 1000, enabled = true): number {
  const [clock] = useState(createClock);
  const subscribe = useCallback((changed: () => void) => (enabled ? clock.run(intervalMs, changed) : () => {}), [clock, intervalMs, enabled]);
  return useSyncExternalStore(subscribe, clock.read, clock.read);
}
