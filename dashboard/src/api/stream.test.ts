import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { SseParser, streamEvents, type StreamStatus } from './stream';
import type { Event } from './types';

function event(seq: number, kind = 'migration.phase', extra: Partial<Event> = {}): Event {
  return {
    seq,
    ts: '2026-10-08T12:00:00Z',
    kind,
    plan_id: 'plan-0a1b2c3d',
    migration_id: 'mig-0123456789',
    actor: 'orchestrator',
    message: `event ${seq}`,
    data: {},
    ...extra,
  } as Event;
}

/** SSE wire format from SDD §12: `id: <seq or 0>\nevent: <kind>\ndata: <Event JSON>\n\n`. */
function frame(e: Event): string {
  return `id: ${e.seq}\nevent: ${e.kind}\ndata: ${JSON.stringify(e)}\n\n`;
}

function sseResponse(chunks: string[], { keepOpen = false } = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      if (!keepOpen) controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const immediate = () => Promise.resolve();

describe('SseParser', () => {
  it('reassembles messages split across arbitrary chunk boundaries, including multi-line data', () => {
    const text =
      'id: 7\r\nevent: migration.phase\r\ndata: {"a":\r\ndata: 1}\r\n\r\n' +
      ': heartbeat\n\n' +
      'id: 0\nevent: migration.progress\ndata: {"b":2}\n\n';
    const parser = new SseParser();
    const messages = [];
    for (let i = 0; i < text.length; i += 3) messages.push(...parser.push(text.slice(i, i + 3)));

    expect(messages).toEqual([
      { id: '7', event: 'migration.phase', data: '{"a":\n1}', retry: null },
      { id: '0', event: 'migration.progress', data: '{"b":2}', retry: null },
    ]);
  });

  it('does not split a CRLF pair that straddles two chunks', () => {
    const parser = new SseParser();
    const messages = [
      ...parser.push('id: 1\r'),
      ...parser.push('\nevent: plan.started\r'),
      ...parser.push('\ndata: {}\r'),
      ...parser.push('\n\r'),
      ...parser.push('\n'),
    ];
    expect(messages).toEqual([{ id: '1', event: 'plan.started', data: '{}', retry: null }]);
  });

  it('ignores comment lines and field-only blocks without data', () => {
    const parser = new SseParser();
    expect(parser.push(': heartbeat\n\n:another comment\n\nevent: noop\n\n')).toEqual([]);
  });

  it('parses retry and strips exactly one leading space from values', () => {
    const parser = new SseParser();
    expect(parser.push('retry: 3000\ndata:  two spaces\n\n')).toEqual([
      { id: null, event: null, data: ' two spaces', retry: 3000 },
    ]);
  });
});

describe('streamEvents', () => {
  it('delivers events, ignores heartbeats, tracks the last id and resumes with since=', async () => {
    const progress = event(0, 'migration.progress', { data: { progress_pct: 41.5 } });
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        sseResponse([': heartbeat\n\n', frame(event(5)), frame(progress), frame(event(6, 'migration.sync_pass'))]),
      )
      // Overlap after reconnect: seq 6 again must not be delivered twice.
      .mockResolvedValueOnce(sseResponse([frame(event(6, 'migration.sync_pass')), frame(event(7))], { keepOpen: true }));
    const client = new ApiClient({ baseUrl: '/api/v1', getToken: () => 'smg_tok', fetchImpl });
    const controller = new AbortController();
    const received: Event[] = [];
    const statuses: StreamStatus[] = [];
    const onHeartbeat = vi.fn();

    const lastSeq = await streamEvents(
      4,
      (e) => {
        received.push(e);
        if (e.seq === 7) controller.abort();
      },
      controller.signal,
      { client, sleep: immediate, onStatus: (s) => statuses.push(s), onHeartbeat },
    );

    expect(lastSeq).toBe(7);
    expect(received.map((e) => [e.seq, e.kind])).toEqual([
      [5, 'migration.phase'],
      [0, 'migration.progress'],
      [6, 'migration.sync_pass'],
      [7, 'migration.phase'],
    ]);
    expect(onHeartbeat).toHaveBeenCalled();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(String(fetchImpl.mock.calls[0]?.[0])).toBe('/api/v1/events/stream?since=4');
    expect(String(fetchImpl.mock.calls[1]?.[0])).toBe('/api/v1/events/stream?since=6');
    const headers = new Headers(fetchImpl.mock.calls[0]?.[1]?.headers);
    expect(headers.get('Authorization')).toBe('Bearer smg_tok');
    expect(headers.get('Accept')).toBe('text/event-stream');
    expect(statuses).toEqual(expect.arrayContaining(['connecting', 'open', 'reconnecting', 'closed']));
  });

  it('connects live-only without since= until it has seen a persisted event', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(sseResponse([frame(event(0, 'migration.progress')), frame(event(41))]))
      .mockResolvedValueOnce(sseResponse([frame(event(42))], { keepOpen: true }));
    const client = new ApiClient({ fetchImpl });
    const controller = new AbortController();

    const lastSeq = await streamEvents(
      null,
      (e) => {
        if (e.seq === 42) controller.abort();
      },
      controller.signal,
      { client, sleep: immediate },
    );

    expect(lastSeq).toBe(42);
    expect(String(fetchImpl.mock.calls[0]?.[0])).toBe('/api/v1/events/stream');
    expect(String(fetchImpl.mock.calls[1]?.[0])).toBe('/api/v1/events/stream?since=41');
  });

  it('treats `heartbeat` events like comments', async () => {
    const heartbeat = event(0, 'heartbeat');
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(sseResponse([frame(heartbeat), frame(event(9))], { keepOpen: true }));
    const client = new ApiClient({ fetchImpl });
    const controller = new AbortController();
    const onEvent = vi.fn((e: Event) => {
      if (e.seq === 9) controller.abort();
    });
    const onHeartbeat = vi.fn();

    await streamEvents(0, onEvent, controller.signal, { client, sleep: immediate, onHeartbeat });

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onHeartbeat).toHaveBeenCalledTimes(1);
  });

  it('skips malformed data without dropping the connection', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(sseResponse(['id: 3\nevent: plan.created\ndata: {not json\n\n', frame(event(4))], { keepOpen: true }));
    const client = new ApiClient({ fetchImpl });
    const controller = new AbortController();
    const received: number[] = [];

    const lastSeq = await streamEvents(
      0,
      (e) => {
        received.push(e.seq);
        controller.abort();
      },
      controller.signal,
      { client, sleep: immediate },
    );

    expect(received).toEqual([4]);
    expect(lastSeq).toBe(4);
  });

  it('backs off and reconnects after network errors', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(sseResponse([frame(event(12))], { keepOpen: true }));
    const client = new ApiClient({ fetchImpl });
    const controller = new AbortController();
    const delays: number[] = [];

    const lastSeq = await streamEvents(11, () => controller.abort(), controller.signal, {
      client,
      sleep: (ms) => {
        delays.push(ms);
        return Promise.resolve();
      },
    });

    expect(lastSeq).toBe(12);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(delays).toHaveLength(2);
    expect(delays[1]).toBeGreaterThan(delays[0] ?? 0);
  });

  it('stops and reports unauthorized on 401', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ error: { code: 'unauthorized', message: 'invalid token' } }), { status: 401 }),
    );
    const onUnauthorized = vi.fn();
    const client = new ApiClient({ fetchImpl, onUnauthorized });
    const statuses: StreamStatus[] = [];

    const lastSeq = await streamEvents(3, vi.fn(), new AbortController().signal, {
      client,
      sleep: immediate,
      onStatus: (s) => statuses.push(s),
    });

    expect(lastSeq).toBe(3);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(statuses.at(-1)).toBe('unauthorized');
  });
});
