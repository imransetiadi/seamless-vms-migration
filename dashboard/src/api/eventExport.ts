import type { ApiClient } from './client';
import type { Event } from './types';

/** The most `GET /events` returns at once (SDD §12). */
const PAGE = 1000;

export interface ExportOptions {
  /** Only this plan's events, like `seamless events export --plan`. */
  planId?: string;
  pageSize?: number;
  signal?: AbortSignal;
  /** How many events have been read so far, after each page. */
  onProgress?: (read: number) => void;
}

/**
 * Every persisted event, oldest first, read page by page with `GET /events?since=` — what
 * `seamless events export` writes (SDD §15, §16). Stopping the signal stops it between pages.
 */
export async function exportEventTrail(api: ApiClient, { planId, pageSize = PAGE, signal, onProgress }: ExportOptions = {}): Promise<Event[]> {
  const out: Event[] = [];
  let since = 0;
  for (;;) {
    signal?.throwIfAborted();
    const page = await api.get<Event[]>('/events', { signal, query: { since, limit: pageSize, plan_id: planId || undefined } });
    out.push(...page);
    onProgress?.(out.length);
    const last = page.at(-1);
    if (page.length < pageSize || !last) return out;
    since = last.seq;
  }
}

/**
 * JSON lines, one event per line — the format of `seamless events export` — as one part per event for a Blob: a
 * trail of millions of events is longer than one string may be.
 */
export function jsonLineParts(events: Event[]): string[] {
  return events.map((event) => `${JSON.stringify(event)}\n`);
}
