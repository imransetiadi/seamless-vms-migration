/**
 * Live events over Server-Sent Events, read with `fetch` streaming (not EventSource) so the
 * bearer token travels in the Authorization header (SDD §12).
 *
 * Wire format: `id: <seq or 0 for ephemeral>\nevent: <kind>\ndata: <Event JSON>\n\n`, plus a
 * `: heartbeat` comment every 15 s. Clients resume with `?since=<last seq>`.
 */
import { ApiClient, ApiError, getDefaultClient, isAbortError } from './client';
import type { Event } from './types';

export interface SseMessage {
  id: string | null;
  event: string | null;
  data: string;
  retry: number | null;
}

/** Incremental parser for the `text/event-stream` format (handles LF, CRLF and CR line endings). */
export class SseParser {
  private buffer = '';
  private pendingCR = false;
  private dataLines: string[] = [];
  private hasData = false;
  private id: string | null = null;
  private event: string | null = null;
  private retry: number | null = null;

  constructor(private readonly onComment?: (text: string) => void) {}

  push(chunk: string): SseMessage[] {
    let text = chunk;
    if (this.pendingCR) {
      // A CRLF pair split across chunks: the CR already ended the line.
      if (text.startsWith('\n')) text = text.slice(1);
      this.pendingCR = false;
    }
    this.buffer += text;

    const messages: SseMessage[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i += 1) {
      const ch = this.buffer[i];
      if (ch !== '\n' && ch !== '\r') continue;
      const line = this.buffer.slice(start, i);
      if (ch === '\r') {
        if (i + 1 < this.buffer.length) {
          if (this.buffer[i + 1] === '\n') i += 1;
        } else {
          this.pendingCR = true;
        }
      }
      start = i + 1;
      const message = this.processLine(line);
      if (message) messages.push(message);
    }
    this.buffer = this.buffer.slice(start);
    return messages;
  }

  private processLine(line: string): SseMessage | null {
    if (line === '') return this.dispatch();
    if (line.startsWith(':')) {
      this.onComment?.(line.slice(1).trim());
      return null;
    }
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    switch (field) {
      case 'data':
        this.dataLines.push(value);
        this.hasData = true;
        break;
      case 'event':
        this.event = value;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
      default:
        break; // unknown fields are ignored per the SSE spec
    }
    return null;
  }

  private dispatch(): SseMessage | null {
    const message: SseMessage | null = this.hasData
      ? { id: this.id, event: this.event, data: this.dataLines.join('\n'), retry: this.retry }
      : null;
    this.dataLines = [];
    this.hasData = false;
    this.id = null;
    this.event = null;
    this.retry = null;
    return message;
  }
}

export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'unauthorized';

/** Silence - no event, no heartbeat - after which a stream counts as dropped: three of the server's 15 s heartbeats (SDD §12, §16). */
export const STREAM_IDLE_MS = 3 * 15_000;

/**
 * How long a connection must stay open to count as a working one, which starts the reconnect delay over: one of the
 * server's heartbeat intervals (SDD §12, §16). One that ends sooner - a proxy that answers 200 and ends the body at
 * once - counts as a failed attempt, so the delay keeps growing instead of a reconnect every second.
 */
export const STREAM_STABLE_MS = 15_000;

/** The wait before the next attempt after a 429: the per-address lockout's minute (SDD §15.1, §16). */
export const STREAM_LOCKOUT_MS = 60_000;

export interface StreamOptions {
  client?: ApiClient;
  onStatus?: (status: StreamStatus) => void;
  onHeartbeat?: () => void;
  /** Injected in tests: the silence after which a stream counts as dropped (default `STREAM_IDLE_MS`). */
  idleTimeoutMs?: number;
  /** Injected in tests: how long a connection must stay open to start the delay over (default `STREAM_STABLE_MS`). */
  stableAfterMs?: number;
  /** Delay before reconnect attempt `n` (0-based). */
  backoff?: (attempt: number) => number;
  /** Injected in tests; must resolve early when `signal` aborts. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export function defaultBackoff(attempt: number): number {
  return Math.min(15_000, 1_000 * 2 ** attempt);
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done, { once: true });
  });
}

function toEvent(message: SseMessage): Event | null {
  let raw: unknown;
  try {
    raw = JSON.parse(message.data);
  } catch {
    return null;
  }
  if (typeof raw !== 'object' || raw === null) return null;
  const e = raw as Partial<Event>;
  const idSeq = Number(message.id ?? 0);
  return {
    seq: typeof e.seq === 'number' ? e.seq : Number.isFinite(idSeq) ? idSeq : 0,
    ts: typeof e.ts === 'string' ? e.ts : new Date().toISOString(),
    kind: typeof e.kind === 'string' ? e.kind : (message.event ?? 'message'),
    plan_id: e.plan_id ?? null,
    migration_id: e.migration_id ?? null,
    actor: typeof e.actor === 'string' ? e.actor : 'system',
    message: typeof e.message === 'string' ? e.message : '',
    data: typeof e.data === 'object' && e.data !== null ? (e.data as Record<string, unknown>) : {},
  };
}

/**
 * Streams events after `since`, reconnecting with backoff and resuming from the last persisted
 * sequence number. Persisted events (seq > 0) are delivered at most once and in order; ephemeral
 * events (seq 0: progress, logs) are passed through; heartbeats are swallowed.
 * `since = null` connects live-only (no `since` parameter) until a persisted event fixes the cursor.
 * Resolves with the last seen sequence number when `signal` aborts, the server rejects the token (401) or refuses
 * the role (403); after a 429 it waits the lockout's minute before trying again (SDD §16).
 */
export async function streamEvents(
  since: number | null,
  onEvent: (event: Event) => void,
  signal: AbortSignal,
  options: StreamOptions = {},
): Promise<number> {
  const client = options.client ?? getDefaultClient();
  const sleep = options.sleep ?? abortableSleep;
  const backoff = options.backoff ?? defaultBackoff;
  const setStatus = (status: StreamStatus) => options.onStatus?.(status);
  const idleMs = options.idleTimeoutMs ?? STREAM_IDLE_MS;
  const stableMs = options.stableAfterMs ?? STREAM_STABLE_MS;

  let cursorKnown = since !== null;
  let lastSeq = since ?? 0;
  let failures = 0;
  let connected = false;

  while (!signal.aborted) {
    setStatus(connected || failures > 0 ? 'reconnecting' : 'connecting');
    // when this attempt's connection opened (null: it never did), and whether the lockout refused it
    let openedAt: number | null = null;
    let lockedOut = false;
    try {
      const response = await client.fetchRaw('/events/stream', {
        query: { since: cursorKnown ? lastSeq : undefined },
        headers: { Accept: 'text/event-stream' },
        cache: 'no-store',
        signal,
      });
      if (!response.body) throw new ApiError(0, 'stream_unsupported', 'This browser cannot read streaming responses.');

      openedAt = Date.now();
      connected = true;
      setStatus('open');

      const reader = response.body.getReader();
      const cancel = () => {
        reader.cancel().catch(() => undefined);
      };
      signal.addEventListener('abort', cancel, { once: true });
      // nothing for three heartbeats: a half-open connection, where no error ever comes - end it and connect again
      // with since=, never "open" on events that stopped (SDD §16)
      let idle: ReturnType<typeof setTimeout> | undefined;
      const watch = () => {
        clearTimeout(idle);
        idle = setTimeout(cancel, idleMs);
      };
      watch();
      const decoder = new TextDecoder();
      const parser = new SseParser((comment) => {
        if (comment === 'heartbeat') options.onHeartbeat?.();
      });

      try {
        while (!signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          watch();
          for (const message of parser.push(decoder.decode(value, { stream: true }))) {
            const event = toEvent(message);
            if (!event) continue;
            if (event.kind === 'heartbeat') {
              options.onHeartbeat?.();
              continue;
            }
            if (event.seq > 0) {
              if (event.seq <= lastSeq) continue; // overlap after a resume
              lastSeq = event.seq;
              cursorKnown = true;
            }
            onEvent(event);
            if (signal.aborted) break;
          }
        }
      } finally {
        clearTimeout(idle);
        signal.removeEventListener('abort', cancel);
      }
    } catch (error) {
      if (signal.aborted || isAbortError(error)) break;
      if (error instanceof ApiError) {
        if (error.status === 401) {
          setStatus('unauthorized');
          return lastSeq;
        }
        // the role will not change: not tried again - Offline, "live updates stopped" (SDD §16)
        if (error.status === 403) break;
        // the per-address lockout (SDD §15.1): one refused request a minute, not one every 15 s
        if (error.status === 429) lockedOut = true;
      }
    }

    if (signal.aborted) break;
    // a connection that stayed open for one heartbeat interval was a working one: the delay starts over; one that
    // ended sooner, whatever it delivered, counts as a failed attempt, as a refused or unreachable one does (SDD §16)
    if (openedAt !== null && Date.now() - openedAt >= stableMs) failures = 0;
    else failures += 1;
    setStatus('reconnecting');
    await sleep(lockedOut ? STREAM_LOCKOUT_MS : backoff(Math.max(0, failures - 1)), signal);
  }

  setStatus('closed');
  return lastSeq;
}
