import type { Stats } from '../api/types';

/** The completed migrations the downtime figures leave out (SDD §5.5, ADV-16), in words; null when none. */
export function leftOutText(stats: Pick<Stats, 'data_inconsistent'> | undefined): string | null {
  const count = stats?.data_inconsistent ?? 0;
  return count > 0 ? `${count} left out: data inconsistent` : null;
}

/** Model seconds per wall second (SDD §7.4): 1 outside demo mode, or when the speed is unknown. */
export function speedOf(demoSpeed: number | null | undefined): number {
  return demoSpeed && demoSpeed > 0 ? demoSpeed : 1;
}

/** The note beside a clock that runs in model time (SDD §16, ADV-16); null at wall speed. */
export function demoTimeText(speed: number): string | null {
  if (speed === 1) return null;
  return speed > 1 ? `Demo time: ${speed}× faster than real time` : `Demo time: ${speed}× real time`;
}
