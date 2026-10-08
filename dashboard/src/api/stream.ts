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

export interface StreamOptions {
  client?: ApiClient;
  onStatus?: (status: StreamStatus) => void;
  onHeartbeat?: () => void;
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
 * Resolves with the last seen sequence number when `signal` aborts or the server rejects the token.
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

  let cursorKnown = since !== null;
  let lastSeq = since ?? 0;
  let failures = 0;
  let connected = false;

  while (!signal.aborted) {
    setStatus(connected || failures > 0 ? 'reconnecting' : 'connecting');
    try {
      const response = await client.fetchRaw('/events/stream', {
        query: { since: cursorKnown ? lastSeq : undefined },
        headers: { Accept: 'text/event-stream' },
        cache: 'no-store',
        signal,
      });
      if (!response.body) throw new ApiError(0, 'stream_unsupported', 'This browser cannot read streaming responses.');

      connected = true;
      failures = 0;
      setStatus('open');

      const reader = response.body.getReader();
      const cancel = () => {
        reader.cancel().catch(() => undefined);
      };
      signal.addEventListener('abort', cancel, { once: true });
      const decoder = new TextDecoder();
      const parser = new SseParser((comment) => {
        if (comment === 'heartbeat') options.onHeartbeat?.();
      });

      try {
        while (!signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
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
        signal.removeEventListener('abort', cancel);
      }
    } catch (error) {
      if (signal.aborted || isAbortError(error)) break;
      if (error instanceof ApiError && error.status === 401) {
        setStatus('unauthorized');
        return lastSeq;
      }
      failures += 1;
    }

    if (signal.aborted) break;
    setStatus('reconnecting');
    await sleep(backoff(Math.max(0, failures - 1)), signal);
  }

  setStatus('closed');
  return lastSeq;
}
