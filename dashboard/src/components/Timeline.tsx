import { Activity, Bot, BrainCircuit, CircleX, Globe, Hand, Network, RefreshCw, Split, ThumbsUp, Timer, TimerOff, Webhook, type LucideIcon } from 'lucide-react';
import type { Event, PhaseChange } from '../api/types';
import { cn } from '../lib/cn';
import { hookKindLabel } from '../lib/cutover';
import { formatDateTime, formatDuration, formatRelative, formatTime } from '../lib/format';
import { phaseMeta, type Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { EmptyState } from './EmptyState';
import { TechPreviewBadge } from './TechPreviewBadge';

interface Entry {
  key: string;
  at: string;
  title: string;
  detail: string | null;
  actor: string;
  icon: LucideIcon;
  tone: Tone;
  /** A step of the network cut-over, which carries its Technology Preview badge (SDD §16, §23.9). */
  network?: boolean;
}

const EVENT_META: Record<string, { icon: LucideIcon; tone: Tone }> = {
  'migration.sync_pass': { icon: RefreshCw, tone: 'progress' },
  'migration.downtime_started': { icon: Timer, tone: 'warning' },
  'migration.downtime_ended': { icon: TimerOff, tone: 'success' },
  'migration.error': { icon: CircleX, tone: 'danger' },
  'migration.approved': { icon: ThumbsUp, tone: 'success' },
  'migration.action': { icon: Hand, tone: 'info' },
};

function eventMeta(kind: string): { icon: LucideIcon; tone: Tone } {
  if (EVENT_META[kind]) return EVENT_META[kind];
  if (kind.startsWith('advisor.')) return { icon: Bot, tone: 'info' };
  if (kind.startsWith('memory.')) return { icon: BrainCircuit, tone: 'info' };
  return { icon: Activity, tone: 'neutral' };
}

/** The network cut-over's steps a migration.action records (SDD §8.2-§8.5), each with its icon. */
const NETWORK_STEPS = new Map<unknown, LucideIcon>([
  ['hook', Webhook],
  ['floating_ip', Network],
  ['dns', Globe],
  ['lb_member', Split],
]);

/** A step's tone by its outcome (SDD §16), never its only sign: its words say the outcome. */
const OUTCOME_TONES = new Map<unknown, Tone>([
  ['succeeded', 'success'],
  ['failed', 'danger'],
  ['conflict', 'warning'],
  ['unchanged', 'warning'],
]);

const HOOK_OUTCOMES = new Map<unknown, string>([
  ['succeeded', 'Succeeded'],
  ['failed', 'Failed'],
]);

/**
 * A network cut-over step's entry (SDD §16): a hook's run by its name, point and outcome, with its kind, duration and
 * detail under it, and a floating IP's, a DNS swap's or an Octavia member's step as its event words it, with the step's
 * detail; null for any other event.
 */
function networkStep(e: Event): Pick<Entry, 'title' | 'detail' | 'icon' | 'tone'> | null {
  const icon = e.kind === 'migration.action' ? NETWORK_STEPS.get(e.data.action) : undefined;
  if (!icon) return null;
  const { outcome } = e.data;
  const tone = OUTCOME_TONES.get(outcome) ?? 'info';
  const detail = typeof e.data.detail === 'string' && e.data.detail ? e.data.detail : null;
  if (e.data.action !== 'hook') return { title: e.message || e.kind, detail, icon, tone };
  const duration = typeof e.data.duration_s === 'number' ? formatDuration(e.data.duration_s) : null;
  return {
    title: `Hook ${String(e.data.hook)} at ${String(e.data.point)}: ${HOOK_OUTCOMES.get(outcome) ?? String(outcome)}`,
    detail: [hookKindLabel(e.data.kind), duration, detail].filter(Boolean).join(' · ') || null,
    icon,
    tone,
  };
}

/**
 * Every phase change (with reason and actor) merged with the migration's audit events, newest first
 * (NFR-09: every migration has a complete timeline).
 */
export function Timeline({ history, events }: { history: PhaseChange[]; events: Event[] }) {
  const entries: Entry[] = [
    ...history.map((h, i) => {
      const meta = phaseMeta(h.to_phase);
      return {
        key: `phase-${i}-${h.at}`,
        at: h.at,
        title: h.from_phase ? `${phaseMeta(h.from_phase).label} → ${meta.label}` : meta.label,
        detail: h.reason,
        actor: h.actor,
        icon: meta.icon,
        tone: meta.tone,
      };
    }),
    ...events
      .filter((e) => e.kind !== 'migration.phase' && e.kind !== 'migration.progress' && e.kind !== 'heartbeat')
      .map((e) => {
        const key = `event-${e.seq}-${e.ts}`;
        const step = networkStep(e);
        if (step) return { key, at: e.ts, actor: e.actor, ...step, network: true };
        const meta = eventMeta(e.kind);
        return { key, at: e.ts, title: e.message || e.kind, detail: e.kind, actor: e.actor, icon: meta.icon, tone: meta.tone };
      }),
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

  if (entries.length === 0) return <EmptyState icon={Activity} title="No history yet" />;

  return (
    <ol aria-label="Timeline, newest first" className="relative flex flex-col">
      {entries.map((entry, index) => {
        const title = <p className="min-w-0 wrap-break-word text-sm font-medium text-foreground">{entry.title}</p>;
        return (
          <li key={entry.key} className="relative flex gap-3 pb-4 last:pb-0">
            {index < entries.length - 1 && <span aria-hidden className="absolute left-[13px] top-7 h-[calc(100%-1.75rem)] w-px bg-border" />}
            <span aria-hidden className={cn('relative mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-card', TONE_CLASSES[entry.tone].text)}>
              <entry.icon className="size-3.5" />
            </span>
            <div className="min-w-0 flex-1">
              {entry.network ? (
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  {title}
                  <TechPreviewBadge feature="network" />
                </div>
              ) : (
                title
              )}
              {entry.detail && <p className="wrap-break-word text-xs text-muted-foreground">{entry.detail}</p>}
              <p className="text-xs text-muted-foreground">
                <time dateTime={entry.at} title={formatDateTime(entry.at)} className="num">
                  {formatTime(entry.at)}
                </time>{' '}
                · {formatRelative(entry.at)} · {entry.actor}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
