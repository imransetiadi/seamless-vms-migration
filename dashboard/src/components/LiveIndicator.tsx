import { LoaderCircle, Radio, ShieldOff, WifiOff, type LucideIcon } from 'lucide-react';
import { useLiveSnapshot, type LiveStatus } from '../api/live';
import { cn } from '../lib/cn';
import { formatTime } from '../lib/format';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';
import { Term } from './Term';

const META: Record<LiveStatus, { label: string; tone: Tone; icon: LucideIcon }> = {
  open: { label: 'Live', tone: 'success', icon: Radio },
  connecting: { label: 'Connecting…', tone: 'warning', icon: LoaderCircle },
  reconnecting: { label: 'Reconnecting…', tone: 'warning', icon: LoaderCircle },
  closed: { label: 'Offline', tone: 'neutral', icon: WifiOff },
  idle: { label: 'Offline', tone: 'neutral', icon: WifiOff },
  unauthorized: { label: 'Signed out', tone: 'danger', icon: ShieldOff },
};

/**
 * Event-stream health, labelled "Live" only while the SSE connection is open. Its label is a focusable term whose
 * detail - when the last event or heartbeat came - shows on hover, focus and tap, never only in a title, so stale
 * data is recognisable without a pointer (SDD §16).
 */
export function LiveIndicator({
  compact = false,
  stacked = false,
  className,
}: {
  compact?: boolean;
  /** The navigation rail's form: the icon above its label, which wraps (SDD §16). */
  stacked?: boolean;
  className?: string;
}) {
  const { status, lastEventAt, lastHeartbeatAt } = useLiveSnapshot();
  const meta = META[status];
  const last = Math.max(lastEventAt ?? 0, lastHeartbeatAt ?? 0);
  const detail = last ? `Last update ${formatTime(last)}` : 'No updates received yet';
  return (
    <Term
      definition={detail}
      className={cn(
        'inline-flex items-center font-medium',
        // a target of its own: 44 px in the phone header and the rail, as the controls beside it; 24 px in a row
        compact
          ? 'min-h-11 min-w-11 justify-center'
          : stacked
            ? 'min-h-11 w-full flex-col justify-center gap-1 text-center text-[11px] leading-tight'
            : 'min-h-6 gap-1.5 whitespace-nowrap text-xs',
        TONE_CLASSES[meta.tone].text,
        className,
      )}
    >
      <meta.icon aria-hidden className={cn('size-3.5 shrink-0', meta.icon === LoaderCircle && 'animate-spin')} />
      {/* the name says what the label is a status of; the visible label stays the short one (WCAG 2.5.3). The space
          between them is text of its own, so the name never relies on CSS to keep the words apart */}
      <span className="sr-only">Event stream:</span>{' '}
      <span className={compact ? 'sr-only' : stacked ? 'max-w-full hyphens-auto [overflow-wrap:anywhere]' : undefined}>{meta.label}</span>
    </Term>
  );
}
