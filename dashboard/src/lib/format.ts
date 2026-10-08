/** Display formatters. Every formatter renders missing / non-finite input as an em dash. */

export const DASH = '—';

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'] as const;

function isNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function pad2(value: number): string {
  return value.toString().padStart(2, '0');
}

function scaled(value: number): string {
  return value >= 100 ? value.toFixed(0) : value.toFixed(1);
}

/** IEC byte sizes: `1023 B`, `1.5 KiB`, `42.5 GiB`, `500 GiB` (three integer digits drop decimals). */
export function formatBytes(bytes: number | null | undefined): string {
  if (!isNumber(bytes)) return DASH;
  const sign = bytes < 0 ? '-' : '';
  let value = Math.abs(bytes);
  if (value < 1024) return `${sign}${Math.round(value)} B`;

  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  let text = scaled(value);
  // Rounding can produce "1024 KiB"; promote to the next unit instead.
  if (Number(text) >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
    text = scaled(value);
  }
  return `${sign}${text} ${BYTE_UNITS[unit]}`;
}

/** Bytes per second (the control plane's `*_bps` fields are bytes/s). */
export function formatRate(bytesPerSecond: number | null | undefined): string {
  if (!isNumber(bytesPerSecond)) return DASH;
  return `${formatBytes(bytesPerSecond)}/s`;
}

/** Human durations: `0.4 s`, `45 s`, `4m 05s`, `2h 03m`, `3d 4h`. Negative values clamp to 0. */
export function formatDuration(seconds: number | null | undefined): string {
  if (!isNumber(seconds)) return DASH;
  const s = Math.max(0, seconds);
  if (s < 10) {
    const tenths = Math.round(s * 10) / 10;
    if (tenths < 10) return `${Number.isInteger(tenths) ? tenths.toFixed(0) : tenths.toFixed(1)} s`;
  }
  const total = Math.round(s);
  if (total < 60) return `${total} s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${pad2(total % 60)}s`;
  if (total < 86_400) return `${Math.floor(total / 3600)}h ${pad2(Math.floor((total % 3600) / 60))}m`;
  return `${Math.floor(total / 86_400)}d ${Math.floor((total % 86_400) / 3600)}h`;
}

/** Running clock: `1:35`, `1:02:03`. */
export function formatClock(seconds: number | null | undefined): string {
  if (!isNumber(seconds)) return DASH;
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(sec)}` : `${m}:${pad2(sec)}`;
}

/** Percentages in [0, 100]; never rounds an incomplete value up to 100% or a non-zero value down to 0%. */
export function formatPct(value: number | null | undefined, decimals = 1): string {
  if (!isNumber(value)) return DASH;
  const v = Math.min(100, Math.max(0, value));
  if (v === 0) return '0%';
  if (v === 100) return '100%';
  const factor = 10 ** decimals;
  let rounded = Math.round(v * factor) / factor;
  if (rounded >= 100) rounded = 100 - 1 / factor;
  if (rounded <= 0) return `<${(1 / factor).toFixed(decimals)}%`;
  return `${rounded.toFixed(decimals).replace(/\.0+$/, '')}%`;
}

export function formatNumber(value: number | null | undefined, maximumFractionDigits = 0): string {
  if (!isNumber(value)) return DASH;
  return new Intl.NumberFormat('en-US', { maximumFractionDigits }).format(value);
}

/** Milliseconds since the epoch for an ISO timestamp / Date / epoch number, or null when invalid. */
export function toMillis(value: string | number | Date | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const ms = value instanceof Date ? value.getTime() : typeof value === 'number' ? value : Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** `just now`, `42 s ago`, `5 min ago`, `3 h ago`, `2 d ago`, `in 5 min`. */
export function formatRelative(
  value: string | number | Date | null | undefined,
  now: number | Date = Date.now(),
): string {
  const t = toMillis(value);
  const n = toMillis(now);
  if (t === null || n === null) return DASH;
  const diff = Math.round((n - t) / 1000);
  const abs = Math.abs(diff);
  if (abs < 5) return 'just now';
  let text: string;
  if (abs < 60) text = `${abs} s`;
  else if (abs < 3600) text = `${Math.floor(abs / 60)} min`;
  else if (abs < 86_400) text = `${Math.floor(abs / 3600)} h`;
  else text = `${Math.floor(abs / 86_400)} d`;
  return diff >= 0 ? `${text} ago` : `in ${text}`;
}

/** Local wall-clock `YYYY-MM-DD HH:MM:SS`. */
export function formatDateTime(value: string | number | Date | null | undefined): string {
  const t = toMillis(value);
  if (t === null) return DASH;
  const d = new Date(t);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

/** Local wall-clock `HH:MM:SS`. */
export function formatTime(value: string | number | Date | null | undefined): string {
  const t = toMillis(value);
  if (t === null) return DASH;
  const d = new Date(t);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Seconds elapsed between two instants (`end` defaults to now); null when `start` is missing. */
export function secondsBetween(
  start: string | number | Date | null | undefined,
  end: string | number | Date | null | undefined = Date.now(),
): number | null {
  const a = toMillis(start);
  const b = toMillis(end ?? Date.now());
  if (a === null || b === null) return null;
  return Math.max(0, (b - a) / 1000);
}
