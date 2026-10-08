import { LoaderCircle, Radio, ShieldOff, WifiOff, type LucideIcon } from 'lucide-react';
import { useLiveSnapshot, type LiveStatus } from '../api/live';
import { cn } from '../lib/cn';
import { formatTime } from '../lib/format';
import type { Tone } from '../lib/phase';
import { TONE_CLASSES } from '../lib/status';

const META: Record<LiveStatus, { label: string; tone: Tone; icon: LucideIcon }> = {
  open: { label: 'Live', tone: 'success', icon: Radio },
  connecting: { label: 'Connecting…', tone: 'warning', icon: LoaderCircle },
  reconnecting: { label: 'Reconnecting…', tone: 'warning', icon: LoaderCircle },
  closed: { label: 'Offline', tone: 'neutral', icon: WifiOff },
  idle: { label: 'Offline', tone: 'neutral', icon: WifiOff },
  unauthorized: { label: 'Signed out', tone: 'danger', icon: ShieldOff },
};

/**
 * Event-stream health, labelled "Live" only while the SSE connection is open; the tooltip shows
 * when the last event/heartbeat arrived so stale data is recognisable.
 */
export function LiveIndicator({ compact = false, className }: { compact?: boolean; className?: string }) {
  const { status, lastEventAt, lastHeartbeatAt } = useLiveSnapshot();
  const meta = META[status];
  const last = Math.max(lastEventAt ?? 0, lastHeartbeatAt ?? 0);
  const detail = last ? `Last update ${formatTime(last)}` : 'No updates received yet';
  return (
    <span
      title={`Event stream: ${meta.label}. ${detail}`}
      className={cn('inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium', TONE_CLASSES[meta.tone].text, className)}
    >
      <meta.icon aria-hidden className={cn('size-3.5 shrink-0', meta.icon === LoaderCircle && 'animate-spin')} />
      <span className={compact ? 'sr-only' : undefined}>
        {compact ? `Event stream: ${meta.label}` : meta.label}
      </span>
    </span>
  );
}
