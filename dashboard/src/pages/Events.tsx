import {
  Activity,
  ArrowRightLeft,
  Bot,
  Download,
  BrainCircuit,
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
import { useId, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useEventTail, usePlans } from '../api/hooks';
import { useLiveEvents } from '../api/live';
import type { Event } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { LiveIndicator } from '../components/LiveIndicator';
import { PageHeader } from '../components/PageHeader';
import { LoadingBlock } from '../components/Skeleton';
import { cn } from '../lib/cn';
import { formatDateTime, formatTime } from '../lib/format';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

const MAX_LIVE = 1000;
const PAGE = 200;

const CATEGORIES: Array<{ value: string; label: string; match: (kind: string) => boolean }> = [
  { value: 'all', label: 'All', match: () => true },
  { value: 'migrations', label: 'Migrations', match: (k) => k.startsWith('migration.') },
  { value: 'plans', label: 'Plans and waves', match: (k) => k.startsWith('plan.') || k.startsWith('wave.') },
  { value: 'advisor', label: 'Advisor and memory', match: (k) => k.startsWith('advisor.') || k.startsWith('memory.') },
  { value: 'providers', label: 'Providers', match: (k) => k.startsWith('provider.') },
  { value: 'security', label: 'Security', match: (k) => k.startsWith('auth.') },
];

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
    default:
      if (kind.startsWith('plan.')) return { icon: ClipboardList, tone: 'info' };
      if (kind.startsWith('wave.')) return { icon: Rows3, tone: 'info' };
      if (kind.startsWith('advisor.')) return { icon: Bot, tone: 'info' };
      if (kind.startsWith('memory.')) return { icon: BrainCircuit, tone: 'info' };
      if (kind.startsWith('provider.')) return { icon: Server, tone: 'neutral' };
      return { icon: Activity, tone: 'neutral' };
  }
}

interface Item {
  key: string;
  event: Event;
}

function byNewest(a: Item, b: Item): number {
  return Date.parse(b.event.ts) - Date.parse(a.event.ts) || b.event.seq - a.event.seq;
}

/** Audit trail plus the live stream: history first, streamed events appended at the top. */
export default function Events() {
  usePageTitle('Events');
  const searchId = useId();
  const history = useEventTail();
  const plans = usePlans();
  const [live, setLive] = useState<Item[]>([]);
  const [held, setHeld] = useState<Item[]>([]);
  const [paused, setPaused] = useState(false);
  const [received, setReceived] = useState(0);
  const [showProgress, setShowProgress] = useState(false);
  const [category, setCategory] = useState('all');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(PAGE);
  const counter = useRef(0);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const progressRef = useRef(showProgress);
  progressRef.current = showProgress;

  // Newest persisted sequence number already loaded: anything at or below it is a replay, not news
  // (a server may replay history when the stream connects without `since`).
  const historyMaxSeq = useMemo(() => (history.data ?? []).reduce((max, e) => Math.max(max, e.seq), 0), [history.data]);
  const historyMaxRef = useRef(historyMaxSeq);
  historyMaxRef.current = historyMaxSeq;
  const seenLive = useRef(new Set<number>());

  useLiveEvents((event) => {
    if (event.kind === 'heartbeat') return;
    if (event.seq === 0 && !progressRef.current) return;
    if (event.seq > 0) {
      if (event.seq <= historyMaxRef.current || seenLive.current.has(event.seq)) return;
      seenLive.current.add(event.seq);
    }
    counter.current += 1;
    const item = { key: event.seq > 0 ? `seq-${event.seq}` : `live-${counter.current}`, event };
    setReceived((n) => n + 1);
    if (pausedRef.current) setHeld((h) => [...h, item].slice(-MAX_LIVE));
    else setLive((l) => [...l, item].slice(-MAX_LIVE));
  });

  const planNames = useMemo(() => new Map((plans.data ?? []).map((p) => [p.id, p.name])), [plans.data]);

  const items = useMemo(() => {
    const merged = new Map<string, Item>();
    for (const event of history.data ?? []) merged.set(`seq-${event.seq}`, { key: `seq-${event.seq}`, event });
    for (const item of live) merged.set(item.key, item);
    return [...merged.values()].sort(byNewest);
  }, [history.data, live]);

  const filtered = useMemo(() => {
    const match = CATEGORIES.find((c) => c.value === category)?.match ?? (() => true);
    const q = query.trim().toLowerCase();
    return items.filter(({ event }) => {
      if (!match(event.kind)) return false;
      if (event.seq === 0 && !showProgress) return false;
      if (!q) return true;
      return [event.message, event.kind, event.actor, event.plan_id ?? '', event.migration_id ?? ''].some((v) => v.toLowerCase().includes(q));
    });
  }, [items, category, query, showProgress]);
  // a failed history load with nothing cached: the trail is unknown, not empty (SDD §16)
  const historyUnknown = Boolean(history.error) && !history.data;

  /** The filtered audit events as JSON lines, oldest first — the format of `seamless events export`. */
  const download = () => {
    const events = filtered
      .map(({ event }) => event)
      .filter((event) => event.seq > 0)
      .sort((a, b) => a.seq - b.seq);
    const blob = new Blob(events.map((event) => `${JSON.stringify(event)}\n`), { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `seamless-events-${new Date().toISOString().slice(0, 19).replace(/:/g, '')}.jsonl`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const resume = () => {
    setLive((l) => [...l, ...held].slice(-MAX_LIVE));
    setHeld([]);
    setPaused(false);
  };

  const statusText = paused
    ? `Paused · ${held.length} new event${held.length === 1 ? '' : 's'} held`
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
              disabledReason={filtered.length ? null : historyUnknown ? 'The audit trail could not be loaded.' : 'No events match the filters.'}
            >
              Download shown events
            </Button>
            {paused ? (
              <Button size="lg" variant="primary" icon={Play} onClick={resume}>
                Resume ({held.length} new)
              </Button>
            ) : (
              <Button size="lg" icon={Pause} onClick={() => setPaused(true)}>
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
          options={CATEGORIES.map((c) => ({ value: c.value, label: c.label }))}
        />
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm text-foreground md:mb-0.5">
          <input type="checkbox" className="size-4 cursor-pointer accent-accent" checked={showProgress} onChange={(e) => setShowProgress(e.target.checked)} />
          Show progress updates
        </label>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-3 text-xs">
        <LiveIndicator />
        <p role="status" aria-label="Event stream" className="text-muted-foreground">
          {statusText}
        </p>
      </div>

      {history.isPending && <LoadingBlock label="Loading the audit trail…" rows={8} />}
      {history.error && <ErrorBanner error={history.error} title="The audit trail is unavailable" onRetry={() => void history.refetch()} />}
      {/* "No events match" only once the trail is known: a failed load is not an empty trail (SDD §16) */}
      {history.data && filtered.length === 0 && (
        <EmptyState icon={ScrollText} title="No events match" description="Change the category or search, or wait for new activity." />
      )}

      {filtered.length > 0 && (
        <ol aria-label="Events, newest first" className="card divide-y divide-border">
          {filtered.slice(0, limit).map(({ key, event }) => {
            const meta = kindMeta(event.kind);
            return (
              <li key={key} className="grid grid-cols-[minmax(0,1fr)] gap-1 px-3 py-2.5 sm:grid-cols-[6.5rem_12.5rem_minmax(0,1fr)] sm:gap-3">
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
                  <p className="flex flex-wrap gap-x-2 text-xs text-muted-foreground">
                    <span>by {event.actor}</span>
                    {event.plan_id && (
                      <Link to={`/plans/${event.plan_id}`} className="underline underline-offset-4">
                        {planNames.get(event.plan_id) ?? event.plan_id}
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
          })}
        </ol>
      )}
      {filtered.length > limit && (
        <div className="mt-3">
          <Button onClick={() => setLimit((n) => n + PAGE)}>Show {Math.min(PAGE, filtered.length - limit)} older events</Button>
        </div>
      )}
    </>
  );
}
