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

// one formatter per precision: building an Intl.NumberFormat costs far more than using one, and tables and tiles
// format thousands of numbers each time a live event redraws them
const numberFormats = new Map<number, Intl.NumberFormat>();

export function formatNumber(value: number | null | undefined, maximumFractionDigits = 0): string {
  if (!isNumber(value)) return DASH;
  let format = numberFormats.get(maximumFractionDigits);
  if (!format) {
    format = new Intl.NumberFormat('en-US', { maximumFractionDigits });
    numberFormats.set(maximumFractionDigits, format);
  }
  return format.format(value);
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

/** Python's order for str, by code point: JavaScript's default sort compares UTF-16 code units, which differs past U+FFFF. */
export function byCodePoint(a: string, b: string): number {
  const x = Array.from(a, (c) => c.codePointAt(0)!);
  const y = Array.from(b, (c) => c.codePointAt(0)!);
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i]! - y[i]!;
  return x.length - y.length;
}

/** "A, B and C", or "A, B, C and 2 more" past `limit` names, as the dashboard lists names in a sentence. */
export function namesText(names: readonly string[], limit = 3): string {
  if (names.length > limit) return `${names.slice(0, limit).join(', ')} and ${names.length - limit} more`;
  return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : (names[0] ?? '');
}

/** Names listed as the API's messages list them: Python's `", ".join(sorted(set(items)))`. */
export function pyNames(items: Iterable<string>): string {
  return [...new Set(items)].sort(byCodePoint).join(', ');
}

/** Control characters as Python's repr escapes them. */
const REPR_ESCAPES: Record<string, string> = { '\\': '\\\\', '\t': '\\t', '\n': '\\n', '\r': '\\r' };

/** What Python's `str.isprintable()` rejects, the ASCII space aside: control, format, surrogate, private-use and unassigned
 * characters and the separators. "Unassigned" is as the browser's Unicode knows it: a character newer than the API's
 * Python (3.11: Unicode 14) is printed here where the API escapes it. */
const NOT_PRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;

/** A character's escape as Python's repr writes it: \xhh, \uhhhh or \Uhhhhhhhh. */
function pyEscape(code: number): string {
  const hex = code.toString(16);
  if (code < 0x100) return `\\x${hex.padStart(2, '0')}`;
  if (code < 0x10000) return `\\u${hex.padStart(4, '0')}`;
  return `\\U${hex.padStart(8, '0')}`;
}

/** Python's whitespace, str.isspace's 29 code points (the same in Python 3.11 and 3.13): what str.strip removes. JavaScript's
 * trim differs - it keeps U+001C..U+001F and U+0085 and removes U+FEFF. */
const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');

/** A text stripped as the API's Python strips it (str.strip): what the API judges blank is blank here too (review RV24-06). */
export function pyStrip(text: string): string {
  return text.replace(PY_STRIP, '');
}

/** The whitespace Python's int() reads around a number: str.isspace's, but U+001C..U+001F - int() keeps an ASCII character as
 * it is and skips only ASCII's own whitespace, where str.strip removes those four too. */
const INT_SPACE = '\\t\\n\\v\\f\\r \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_INT = new RegExp(`^[${INT_SPACE}]*([+-]?)(\\p{Nd}+(?:_\\p{Nd}+)*)[${INT_SPACE}]*$`, 'u');
const DECIMAL = /^\p{Nd}$/u;

/** A decimal digit's value in any script: Unicode assigns decimal digits in runs of ten, zero first. */
function decimal(ch: string): number {
  const code = ch.codePointAt(0)!;
  let zero = code;
  while (DECIMAL.test(String.fromCodePoint(zero - 1))) zero -= 1;
  return (code - zero) % 10;
}

/** A text as Python's int() reads it - its whitespace around it, a sign, underscores between digits and any script's decimal
 * digits -, else null: how Nova reads a flavor's amounts and counts (SDD §22.2, PR #64 review). */
export function pyInt(text: string): number | null {
  const match = PY_INT.exec(text);
  if (!match) return null;
  let n = 0;
  for (const ch of match[2]!) if (ch !== '_') n = n * 10 + decimal(ch);
  return match[1] === '-' ? -n : n;
}

/** A text's length as the API's Python counts it, in code points: an emoji is one where JavaScript's length counts its
 * two UTF-16 units - a minimum the dashboard checks is the API's (SDD §14.6, audit AD-10). */
export function pyLen(text: string): number {
  return [...text].length;
}

/** Why a text the API caps at `max` characters is too long to send - stripped as its Python strips it, counted in code
 * points -, else null: a dialog refuses it, saying so, as the browser's maxLength counts UTF-16 units (review RV50-06). */
export function pyTooLong(text: string, max: number): string | null {
  return pyLen(pyStrip(text)) > max ? `At most ${max} characters.` : null;
}

/**
 * A float as the API's messages print it, Python's `repr`: the shortest digits that read back, as JavaScript's, written
 * out from 1e-4 to below 1e16 - a whole number with ".0" - and otherwise with an exponent of at least two digits.
 */
export function pyFloat(value: number): string {
  if (Number.isNaN(value)) return 'nan';
  if (!Number.isFinite(value)) return value > 0 ? 'inf' : '-inf';
  if (value === 0) return Object.is(value, -0) ? '-0.0' : '0.0';
  const [digits, exponent] = value.toExponential().split('e') as [string, string];
  const power = Number(exponent);
  if (power < -4 || power >= 16) return `${digits}e${power < 0 ? '-' : '+'}${String(Math.abs(power)).padStart(2, '0')}`;
  const plain = value.toFixed(Math.max(0, (digits.split('.')[1]?.length ?? 0) - power));
  return plain.includes('.') ? plain : `${plain}.0`;
}

/**
 * A name quoted as the API's messages quote it, Python's `repr` of a str: in single quotes, in double quotes when it
 * holds a single quote and no double one, with backslashes escaped and what Python does not print written as an escape.
 */
export function pyRepr(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let body = '';
  for (const ch of value) {
    if (ch in REPR_ESCAPES) body += REPR_ESCAPES[ch];
    else if (ch === quote) body += `\\${ch}`;
    else if (ch !== ' ' && NOT_PRINTABLE.test(ch)) body += pyEscape(ch.codePointAt(0)!);
    else body += ch;
  }
  return quote + body + quote;
}
