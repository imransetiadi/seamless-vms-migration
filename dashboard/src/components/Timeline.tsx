import { Activity, Bot, BrainCircuit, CircleX, Hand, RefreshCw, ThumbsUp, Timer, TimerOff, type LucideIcon } from 'lucide-react';
import type { Event, PhaseChange } from '../api/types';
import { cn } from '../lib/cn';
import { formatDateTime, formatRelative, formatTime } from '../lib/format';
import { phaseMeta, type Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { EmptyState } from './EmptyState';

interface Entry {
  key: string;
  at: string;
  title: string;
  detail: string | null;
  actor: string;
  icon: LucideIcon;
  tone: Tone;
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
        const meta = eventMeta(e.kind);
        return { key: `event-${e.seq}-${e.ts}`, at: e.ts, title: e.message || e.kind, detail: e.kind, actor: e.actor, icon: meta.icon, tone: meta.tone };
      }),
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

  if (entries.length === 0) return <EmptyState icon={Activity} title="No history yet" />;

  return (
    <ol aria-label="Timeline, newest first" className="relative flex flex-col">
      {entries.map((entry, index) => (
        <li key={entry.key} className="relative flex gap-3 pb-4 last:pb-0">
          {index < entries.length - 1 && <span aria-hidden className="absolute left-[13px] top-7 h-[calc(100%-1.75rem)] w-px bg-border" />}
          <span aria-hidden className={cn('relative mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-card', TONE_CLASSES[entry.tone].text)}>
            <entry.icon className="size-3.5" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="break-words text-sm font-medium text-foreground">{entry.title}</p>
            {entry.detail && <p className="break-words text-xs text-muted-foreground">{entry.detail}</p>}
            <p className="text-xs text-muted-foreground">
              <time dateTime={entry.at} title={formatDateTime(entry.at)} className="num">
                {formatTime(entry.at)}
              </time>{' '}
              · {formatRelative(entry.at)} · {entry.actor}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
