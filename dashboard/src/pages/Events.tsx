import { useListState } from '../lib/useListState';
import {
  Activity,
  Archive,
  ArrowRightLeft,
  Bot,
  Download,
  Eraser,
  BrainCircuit,
  Gavel,
  CircleStop,
  CircleX,
  ClipboardList,
  Hand,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Rows3,
  ScrollText,
  Server,
  ShieldAlert,
  ThumbsUp,
  Timer,
  TimerOff,
  type LucideIcon,
} from 'lucide-react';
import { memo, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { exportEventTrail, jsonLineParts } from '../api/eventExport';
import { fetchEventTail, useApi, useEventTail, usePlans } from '../api/hooks';
import { useLiveEvents, useLiveStatus } from '../api/live';
import type { Event } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { LiveIndicator } from '../components/LiveIndicator';
import { PageHeader } from '../components/PageHeader';
import { LoadingBlock } from '../components/Skeleton';
import { cn } from '../lib/cn';
import { EVENT_CATEGORIES } from '../lib/eventCategories';
import { formatDateTime, formatNumber, formatTime } from '../lib/format';
import type { Tone } from '../lib/phase';
import { createSeenSeqs } from '../lib/seen';
import { TONE_CLASSES } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

const MAX_LIVE = 1000;
const PAGE = 200;
/** Events per history request: the page loads this many, and Load older events this many more (SDD §16). */
const HISTORY = 500;
const NO_ITEMS: Item[] = [];


function kindMeta(kind: string): { icon: LucideIcon; tone: Tone } {
  switch (kind) {
    case 'migration.phase':
      return { icon: ArrowRightLeft, tone: 'info' };
    case 'migration.sync_pass':
      return { icon: RefreshCw, tone: 'progress' };
    case 'migration.downtime_started':
      return { icon: Timer, tone: 'warning' };
    case 'migration.downtime_ended':
      return { icon: TimerOff, tone: 'success' };
    case 'migration.error':
      return { icon: CircleX, tone: 'danger' };
    case 'migration.approved':
      return { icon: ThumbsUp, tone: 'success' };
    case 'migration.action':
      return { icon: Hand, tone: 'info' };
    case 'migration.created':
      return { icon: Plus, tone: 'neutral' };
    case 'migration.progress':
      return { icon: Activity, tone: 'progress' };
    case 'migration.log':
      return { icon: ScrollText, tone: 'neutral' };
    case 'auth.denied':
      return { icon: ShieldAlert, tone: 'danger' };
    case 'events.pruned':
      return { icon: Eraser, tone: 'warning' };
    default:
      if (kind.startsWith('plan.')) return { icon: ClipboardList, tone: 'info' };
      if (kind.startsWith('wave.')) return { icon: Rows3, tone: 'info' };
      if (kind.startsWith('advisor.') || kind.startsWith('recommendation.')) return { icon: Bot, tone: 'info' };
      if (kind.startsWith('decision.')) return { icon: Gavel, tone: 'info' };
      if (kind.startsWith('memory.')) return { icon: BrainCircuit, tone: 'info' };
      if (kind.startsWith('provider.')) return { icon: Server, tone: 'neutral' };
      return { icon: Activity, tone: 'neutral' };
  }
}

interface Item {
  key: string;
  event: Event;
  /** `event.ts` in ms, parsed once: the list is sorted again on every live event. */
  t: number;
}

function itemOf(key: string, event: Event): Item {
  return { key, event, t: Date.parse(event.ts) };
}

function byNewest(a: Item, b: Item): number {
  return b.t - a.t || b.event.seq - a.event.seq;
}

/**
 * One event. Memoized on the event and its plan's name: a live event redraws the page's count and adds its own row,
 * so the rows already shown (up to a page of 200) are not drawn again (SDD §16).
 */
const EventRow = memo(function EventRow({ event, planName }: { event: Event; planName: string | undefined }) {
  const meta = kindMeta(event.kind);
  return (
    <li className="grid grid-cols-[minmax(0,1fr)] gap-1 px-3 py-2.5 sm:grid-cols-[6.5rem_12.5rem_minmax(0,1fr)] sm:gap-3">
      <time dateTime={event.ts} title={formatDateTime(event.ts)} className="num text-xs text-muted-foreground">
        {formatTime(event.ts)}
        <span className="block text-[11px]">{formatDateTime(event.ts).slice(0, 10)}</span>
      </time>
      <span className={cn('inline-flex min-w-0 items-start gap-1.5 text-xs', TONE_CLASSES[meta.tone].text)}>
        <meta.icon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
        <code className="break-all text-foreground">{event.kind}</code>
      </span>
      <div className="min-w-0">
        <p className="wrap-break-word text-sm text-foreground">{event.message || '—'}</p>
        <p className="flex flex-wrap gap-2 text-xs text-muted-foreground">
          <span>by {event.actor}</span>
          {event.plan_id && (
            <Link to={`/plans/${event.plan_id}`} className="underline underline-offset-4">
              {planName ?? event.plan_id}
            </Link>
          )}
          {event.migration_id && (
            <Link to={`/migrations/${event.migration_id}`} className="font-mono underline underline-offset-4">
              {event.migration_id}
            </Link>
          )}
          {event.seq > 0 && <span className="num">#{event.seq}</span>}
        </p>
      </div>
    </li>
  );
});

/** Saves JSON lines as a file, the format of `seamless events export`. */
function saveJsonLines(lines: string[], name: string): void {
  const blob = new Blob(lines, { type: 'application/x-ndjson' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

function stamp(): string {
  return new Date().toISOString().slice(0, 19).replace(/:/g, '');
}

/** Audit trail plus the live stream: history first, streamed events appended at the top. */
export default function Events() {
  usePageTitle('Events');
  const streamStatus = useLiveStatus();
  const searchId = useId();
  const listState = useListState('events');
  // the plan filter lives in the address like Overview's (SDD §16): its history comes from the API
  const [params, setParams] = useSearchParams();
  const planId = params.get('plan') ?? '';
  const history = useEventTail(planId ? { plan_id: planId } : {}, HISTORY);
  const plans = usePlans();
  const [live, setLive] = useState<Item[]>([]);
  const [held, setHeld] = useState<Item[]>([]);
  const [paused, setPaused] = listState.field('paused', false);
  const [received, setReceived] = useState(0);
  // only pausing and resuming are announced: a region that read every event out would flood a screen reader (SDD §16)
  const [announcement, setAnnouncement] = useState('');
  const [showProgress, setShowProgress] = listState.field('showProgress', false);
  const [category, setCategory] = listState.field('category', 'all');
  const [query, setQuery] = listState.field('query', '');
  const searchRef = useRef<HTMLInputElement>(null);
  const filtersActive = query !== '' || category !== 'all' || planId !== '' || showProgress;
  const clearFilters = () => {
    setQuery('');
    setCategory('all');
    setShowProgress(false);
    const next = new URLSearchParams(params);
    next.delete('plan');
    setParams(next, { replace: true });
    setLimit(PAGE);
    searchRef.current?.focus();
  };
  const [limit, setLimit] = listState.field('limit', PAGE);
  const counter = useRef(0);
  const listRef = useRef<HTMLOListElement>(null);

  // Newest persisted sequence number already loaded: anything at or below it is a replay, not news
  // (a server may replay history when the stream connects without `since`).
  const historyMaxSeq = useMemo(() => (history.data ?? []).reduce((max, e) => Math.max(max, e.seq), 0), [history.data]);
  // the persisted live events shown, by sequence, so a replay is shown once - bounded: an open page keeps no more
  // numbers than four times the live list it shows (perf audit PF-07)
  const seenLive = useRef(createSeenSeqs(4 * MAX_LIVE));

  // the listener of the last render (an Effect Event, useLiveEvents): it reads the page's state as it is now
  useLiveEvents((event) => {
    if (event.kind === 'heartbeat') return;
    if (event.seq === 0 && !showProgress) return;
    if (event.seq > 0) {
      if (event.seq <= historyMaxSeq || !seenLive.current.add(event.seq)) return;
    }
    counter.current += 1;
    const item = itemOf(event.seq > 0 ? `seq-${event.seq}` : `live-${counter.current}`, event);
    setReceived((n) => n + 1);
    if (paused) setHeld((h) => [...h, item].slice(-MAX_LIVE));
    else setLive((l) => [...l, item].slice(-MAX_LIVE));
  });

  const planNames = useMemo(() => new Map((plans.data ?? []).map((p) => [p.id, p.name])), [plans.data]);

  const historyItems = useMemo(() => (history.data ?? []).map((event) => itemOf(`seq-${event.seq}`, event)), [history.data]);
  // pages before the loaded history (SDD §12 `before`), for the plan they were read for; a full page may have more
  const [older, setOlder] = listState.field<{ planId: string | null; items: Item[]; more: boolean }>('older', { planId: null, items: [], more: true });
  const olderItems = older.planId === planId ? older.items : NO_ITEMS;
  const items = useMemo(() => {
    const merged = new Map<string, Item>();
    for (const item of olderItems) merged.set(item.key, item);
    for (const item of historyItems) merged.set(item.key, item);
    for (const item of live) merged.set(item.key, item);
    return [...merged.values()].sort(byNewest);
  }, [olderItems, historyItems, live]);

  const filtered = useMemo(() => {
    const match = EVENT_CATEGORIES.find((c) => c.value === category)?.match ?? (() => true);
    const q = query.trim().toLowerCase();
    return items.filter(({ event }) => {
      if (planId && event.plan_id !== planId) return false;
      if (!match(event.kind)) return false;
      if (event.seq === 0 && !showProgress) return false;
      if (!q) return true;
      return [event.message, event.kind, event.actor, event.plan_id ?? '', event.migration_id ?? ''].some((v) => v.toLowerCase().includes(q));
    });
  }, [items, category, query, showProgress, planId]);
  // a failed history load with nothing cached: the trail is unknown, not empty (SDD §16)
  const historyUnknown = Boolean(history.error) && !history.data;
  // the shown audit events (SDD §16): the page as shown, without progress updates (never persisted)
  const shownAudit = useMemo(() => filtered.slice(0, limit).filter(({ event }) => event.seq > 0), [filtered, limit]);
  const downloadReason = shownAudit.length
    ? null
    : historyUnknown
      ? 'The audit trail could not be loaded.'
      : filtered.length
        ? 'Only progress updates are shown; they are not part of the audit trail.'
        : 'No events match the filters.';

  /** The shown audit events as JSON lines, oldest first — the format of `seamless events export`. */
  const download = () => {
    const events = shownAudit.map(({ event }) => event).sort((a, b) => a.seq - b.seq);
    saveJsonLines(jsonLineParts(events), `seamless-events-${stamp()}.jsonl`);
  };

  // the whole trail (SDD §16): every persisted event, the chosen plan's when one is, paged like
  // `seamless events export`; the same button stops it, so the focus stays where it is
  const api = useApi();

  // Load older events (SDD §12, §16): the page before the oldest event loaded, for the chosen plan; the same
  // button as Show older events, so the focus stays on it
  const moreRef = useRef<HTMLButtonElement>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<unknown>(null);
  const canLoadOlder = Boolean(history.data) && (older.planId === planId ? older.more : (history.data?.length ?? 0) >= HISTORY);
  const loadOlder = async () => {
    const oldest = [...olderItems, ...historyItems].reduce((min, { event }) => (event.seq > 0 ? Math.min(min, event.seq) : min), Infinity);
    if (!Number.isFinite(oldest)) return;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await fetchEventTail(api, { plan_id: planId || undefined, before: oldest }, HISTORY);
      const restoreFocus = document.activeElement === moreRef.current;
      const pageItems = page.map((event) => itemOf(`seq-${event.seq}`, event));
      // the history it was read below stays loaded too: when the newest history is read again, its window moves up
      // and would otherwise leave a gap between it and these pages
      setOlder((prev) => ({ planId, items: [...pageItems, ...(prev.planId === planId ? prev.items : []), ...historyItems], more: page.length >= HISTORY }));
      setLimit((n) => n + PAGE);
      setAnnouncement(page.length ? `Loaded ${page.length} older events.` : 'This is the start of the trail.');
      // the start of the trail removes the button: focus moves to the list, never to the page (SDD §16)
      requestAnimationFrame(() => {
        if (restoreFocus && !moreRef.current && document.activeElement === document.body) (listRef.current ?? searchRef.current)?.focus();
      });
    } catch (error) {
      setOlderError(error);
    } finally {
      setLoadingOlder(false);
    }
  };
  const [exported, setExported] = useState<number | null>(null);
  const [exportError, setExportError] = useState<unknown>(null);
  const exportRun = useRef<AbortController | null>(null);
  // leaving the page stops an export: nothing is saved behind the operator's back
  useEffect(() => () => exportRun.current?.abort(), []);
  const exportTrail = async () => {
    if (exportRun.current) {
      exportRun.current.abort();
      return;
    }
    const run = new AbortController();
    exportRun.current = run;
    setExported(0);
    setExportError(null);
    try {
      const events = await exportEventTrail(api, { planId: planId || undefined, signal: run.signal, onProgress: setExported });
      saveJsonLines(jsonLineParts(events), `seamless-events-${planId ? `${planId}-` : ''}all-${stamp()}.jsonl`);
      setAnnouncement(`Exported ${events.length} events.`);
    } catch (error) {
      if (run.signal.aborted) setAnnouncement('Export stopped: nothing was saved.');
      else setExportError(error);
    } finally {
      exportRun.current = null;
      setExported(null);
    }
  };

  const pause = () => {
    setPaused(true);
    setAnnouncement('Live updates paused: new events wait until you resume.');
  };

  const resume = () => {
    setLive((l) => [...l, ...held].slice(-MAX_LIVE));
    setHeld([]);
    setPaused(false);
    setAnnouncement(
      held.length === 0
        ? 'Live updates resumed: no new event arrived while paused.'
        : `Live updates resumed: ${held.length} held event${held.length === 1 ? '' : 's'} added.`,
    );
  };

  // Pause stops everything that updates on its own — the list and this count — until Resume (WCAG 2.2.2, SDD §16)
  const statusText = paused
    ? 'Paused · new events wait until you resume'
    : streamStatus !== 'open'
      ? `${streamStatus === 'connecting' ? 'Connecting' : streamStatus === 'reconnecting' ? 'Reconnecting' : streamStatus === 'unauthorized' ? 'Signed out' : 'Offline'} · showing last loaded events`
    : received === 0
      ? 'Live · waiting for new events'
      : `Live · ${received} new event${received === 1 ? '' : 's'} since you opened this page`;

  return (
    <>
      <PageHeader
        title="Events"
        description="The audit trail of every plan, migration, advisor and provider action, followed live."
        actions={
          <div className="flex flex-wrap gap-2">
            <Button
              size="lg"
              icon={Download}
              onClick={download}
              disabledReason={downloadReason}
            >
              Download shown events
            </Button>
            <Button size="lg" icon={exported === null ? Archive : CircleStop} onClick={() => void exportTrail()}>
              {exported === null ? 'Export the whole trail' : 'Stop export'}
            </Button>
            {paused ? (
              <Button size="lg" variant="primary" icon={Play} onClick={resume}>
                Resume live updates
              </Button>
            ) : (
              <Button size="lg" icon={Pause} onClick={pause}>
                Pause live updates
              </Button>
            )}
          </div>
        }
      />

      <div className="mb-3 grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-end">
        <div className="col-span-2 min-w-0 md:w-72">
          <label htmlFor={searchId} className="field-label">
            Search events
          </label>
          <input
            ref={searchRef}
            id={searchId}
            type="search"
            className="input"
            placeholder="Message, VM, actor, kind or id"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setLimit(PAGE);
            }}
          />
        </div>
        <SelectField
          label="Category"
          className="md:w-52"
          value={category}
          onChange={(e) => {
            setCategory(e.target.value);
            setLimit(PAGE);
          }}
          options={EVENT_CATEGORIES.map((c) => ({ value: c.value, label: c.label }))}
        />
        <SelectField
          label="Plan"
          className="md:w-60"
          value={planId}
          onChange={(e) => {
            const next = new URLSearchParams(params);
            if (e.target.value) next.set('plan', e.target.value);
            else next.delete('plan');
            setParams(next, { replace: true });
            setLimit(PAGE);
          }}
          options={[
            { value: '', label: 'All plans' },
            ...(plans.data ?? []).map((p) => ({ value: p.id, label: p.name })),
            // a plan in the address that the list does not hold (not loaded, or gone) stays chosen
            ...(planId && !(plans.data ?? []).some((p) => p.id === planId) ? [{ value: planId, label: planId }] : []),
          ]}
        />
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-foreground md:mb-0.5">
          <input type="checkbox" className="size-4 cursor-pointer accent-accent" checked={showProgress} onChange={(e) => setShowProgress(e.target.checked)} />
          Show progress updates
        </label>
        {filtersActive && <Button size="lg" onClick={clearFilters}>Clear filters</Button>}
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-3 text-xs">
        <LiveIndicator />
        <p className="text-muted-foreground">{statusText}</p>
        {exported !== null && (
          // plain text: read on demand, never announced page by page; the result is (SDD §16)
          <p className="text-muted-foreground">Exporting the whole trail: {formatNumber(exported)} events read</p>
        )}
        <p role="status" className="sr-only">
          {announcement}
        </p>
      </div>

      {history.isPending && <LoadingBlock label="Loading the audit trail…" rows={8} />}
      {history.error && <ErrorBanner error={history.error} title="The audit trail is unavailable" onRetry={() => void history.refetch()} />}
      {/* the plan filter is built from the plans: without them it lists only the chosen one (SDD §16) */}
      {plans.error && <ErrorBanner error={plans.error} title="Plans are unavailable: the plan filter lists only the chosen plan" onRetry={() => void plans.refetch()} />}
      {exportError !== null && <ErrorBanner error={exportError} title="The export failed: nothing was saved" onRetry={() => void exportTrail()} />}
      {/* "No events match" only once the trail is known: a failed load is not an empty trail; read out from a live
          region already on the page — the count is not, as it moves with every live event (SDD §16) */}
      <div role="status">
        {history.data && filtered.length === 0 && (
          <EmptyState icon={ScrollText} title="No events match" description="Change the category or search, or wait for new activity." />
        )}
      </div>

      {filtered.length > 0 && (
        <ol ref={listRef} tabIndex={-1} aria-label="Events, newest first" className="card divide-y divide-border outline-hidden">
          {filtered.slice(0, limit).map(({ key, event }) => (
            <EventRow key={key} event={event} planName={event.plan_id ? planNames.get(event.plan_id) : undefined} />
          ))}
        </ol>
      )}
      {(filtered.length > limit || canLoadOlder) && (
        <div className="mt-3">
          <Button
            ref={moreRef}
            disabledReason={loadingOlder ? 'Loading older events…' : null}
            onClick={() => {
              if (filtered.length <= limit) {
                void loadOlder();
                return;
              }
              // the last page removes this button, unless older events can still be loaded: focus moves to the
              // list, never to the page (SDD §16)
              if (filtered.length <= limit + PAGE && !canLoadOlder) listRef.current?.focus();
              setLimit((n) => n + PAGE);
            }}
          >
            {filtered.length > limit ? `Show ${Math.min(PAGE, filtered.length - limit)} older events` : 'Load older events'}
          </Button>
        </div>
      )}
      {olderError !== null && <ErrorBanner error={olderError} title="Older events could not be loaded" onRetry={() => void loadOlder()} />}
    </>
  );
}
