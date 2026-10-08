/** Clean axis ticks (0 / 50 / 100 / 150 MiB/s, 0 / 5 / 10 min) instead of arbitrary fractions. */

const MANTISSAS = [1, 2, 2.5, 5, 10];

export function niceStep(raw: number): number {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1;
  const base = 10 ** Math.floor(Math.log10(raw));
  for (const m of MANTISSAS) if (m * base >= raw - 1e-9) return m * base;
  return 10 * base;
}

function ticksFor(max: number, step: number): number[] {
  const top = Math.max(step, Math.ceil((max - 1e-9) / step) * step);
  const ticks: number[] = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

export function niceTicks(max: number, count = 4): number[] {
  return ticksFor(Math.max(0, max), niceStep(Math.max(0, max) / count));
}

/** Ticks for byte quantities: nice numbers in the unit they will be displayed in. */
export function niceByteTicks(max: number, count = 4): number[] {
  const unit = [1024 ** 4, 1024 ** 3, 1024 ** 2, 1024].find((u) => max >= u) ?? 1;
  return niceTicks(max / unit, count).map((v) => v * unit);
}

const DURATION_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10_800, 21_600, 43_200, 86_400];

export function niceDurationTicks(max: number, count = 4): number[] {
  const raw = Math.max(0, max) / count;
  const step = DURATION_STEPS.find((s) => s >= raw) ?? Math.ceil(raw / 86_400) * 86_400;
  return ticksFor(Math.max(0, max), step);
}

/** Compact duration for axis ticks: `45 s`, `5 min`, `5m 30s`, `2 h`, `1h 30m`. */
export function formatDurationTick(seconds: number): string {
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return s % 60 === 0 ? `${s / 60} min` : `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m === 0 ? `${h} h` : `${h}h ${m}m`;
}
