import type { LucideIcon } from 'lucide-react';
import {
  BadgeCheck,
  Ban,
  CircleCheck,
  CircleHelp,
  CirclePlay,
  CircleSlash,
  CircleX,
  Clock,
  Copy,
  History,
  Hourglass,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
  Undo2,
  Zap,
} from 'lucide-react';
import type { Migration, Phase, Strategy, SyncPass } from '../api/types';

/** Semantic status tones. Colour is always paired with an icon and a text label (SDD §16). */
export const TONES = ['neutral', 'info', 'progress', 'warning', 'success', 'danger'] as const;
export type Tone = (typeof TONES)[number];

export interface PhaseMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
  /** One sentence for tooltips, steppers and screen readers. */
  description: string;
}

/*
 * Tone semantics: progress = the machine is working, warning = waiting on a human or recovering,
 * danger = broken, success = done, info = idle but healthy, neutral = not started / inert.
 */
const PHASE_META: Record<Phase, PhaseMeta> = {
  pending: {
    label: 'Pending',
    tone: 'neutral',
    icon: Clock,
    description: 'Created; waiting for pre-flight validation.',
  },
  validating: {
    label: 'Validating',
    tone: 'info',
    icon: ScanSearch,
    description: 'Running pre-flight checks and downtime estimates.',
  },
  blocked: {
    label: 'Blocked',
    tone: 'danger',
    icon: Ban,
    description: 'A blocker finding prevents migration; fix it and re-validate.',
  },
  ready: {
    label: 'Ready',
    tone: 'info',
    icon: CirclePlay,
    description: 'Validated; waits for its wave to start.',
  },
  precopy: {
    label: 'Pre-copy',
    tone: 'progress',
    icon: Copy,
    description: 'First full copy while the source VM keeps running.',
  },
  syncing: {
    label: 'Syncing',
    tone: 'progress',
    icon: RefreshCw,
    description: 'Delta passes while the source VM keeps running.',
  },
  awaiting_cutover: {
    label: 'Awaiting cutover',
    tone: 'warning',
    icon: Hourglass,
    description: 'Converged; waiting for approval, the cutover window or a cutover request.',
  },
  cutover: {
    label: 'Cutover',
    tone: 'progress',
    icon: Zap,
    description: 'Source VM stopped: final sync and boot on RHOSO. The downtime clock is running.',
  },
  verifying: {
    label: 'Verifying',
    tone: 'progress',
    icon: ShieldCheck,
    description: 'Checking the destination server, ports, TCP probes and console output.',
  },
  completed: {
    label: 'Completed',
    tone: 'success',
    icon: CircleCheck,
    description: 'Running on RHOSO; still reversible until finalized.',
  },
  finalized: {
    label: 'Finalized',
    tone: 'success',
    icon: BadgeCheck,
    description: 'Source cleaned up; rollback is no longer possible.',
  },
  failed: {
    label: 'Failed',
    tone: 'danger',
    icon: CircleX,
    description: 'A step failed; retry, roll back or cancel.',
  },
  rolling_back: {
    label: 'Rolling back',
    tone: 'warning',
    icon: Undo2,
    description: 'Removing the destination instance and restarting the source VM.',
  },
  rolled_back: {
    label: 'Rolled back',
    tone: 'warning',
    icon: History,
    description: 'The source VM is running again; retry when the cause is fixed.',
  },
  cancelled: {
    label: 'Cancelled',
    tone: 'neutral',
    icon: CircleSlash,
    description: 'Removed from the plan; nothing further will run.',
  },
};

export function phaseMeta(phase: Phase): PhaseMeta {
  return (
    PHASE_META[phase] ?? {
      label: String(phase),
      tone: 'neutral',
      icon: CircleHelp,
      description: 'Phase not known to this dashboard version.',
    }
  );
}

/** Lifecycle order used for distributions and sorting. */
export const PHASE_ORDER: readonly Phase[] = [
  'pending',
  'validating',
  'blocked',
  'ready',
  'precopy',
  'syncing',
  'awaiting_cutover',
  'cutover',
  'verifying',
  'completed',
  'finalized',
  'failed',
  'rolling_back',
  'rolled_back',
  'cancelled',
];

const TERMINAL: ReadonlySet<Phase> = new Set(['finalized', 'cancelled']);
/** Phases driven by an orchestrator task (resumed after a restart, SDD §8). */
const ACTIVE: ReadonlySet<Phase> = new Set(['precopy', 'syncing', 'cutover', 'verifying', 'rolling_back']);
/** Phases during which the downtime clock may be running. */
const DOWNTIME: ReadonlySet<Phase> = new Set(['cutover', 'verifying', 'rolling_back']);

export function isTerminalPhase(phase: Phase): boolean {
  return TERMINAL.has(phase);
}

export function isActivePhase(phase: Phase): boolean {
  return ACTIVE.has(phase);
}

export function isDowntimePhase(phase: Phase): boolean {
  return DOWNTIME.has(phase);
}

export function isWarmStrategy(strategy: Strategy): boolean {
  return strategy === 'warm' || strategy === 'vmware_warm';
}

/** The success path for a strategy family (SDD §5.2). */
export function happyPath(strategy: Strategy): Phase[] {
  return isWarmStrategy(strategy)
    ? ['pending', 'validating', 'ready', 'precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'completed', 'finalized']
    : ['pending', 'validating', 'ready', 'cutover', 'verifying', 'completed', 'finalized'];
}

export function phaseSortKey(phase: Phase): number {
  const index = PHASE_ORDER.indexOf(phase);
  return index === -1 ? PHASE_ORDER.length : index;
}

/**
 * Whether a migration's pre-flight ran: validation finished it (SDD §8, §16). Pending and validating
 * migrations have not been checked yet, nor has one cancelled before its validation finished.
 */
export function preflightRan(m: Pick<Migration, 'phase' | 'phase_history'>): boolean {
  if (m.phase === 'pending' || m.phase === 'validating') return false;
  if (m.phase !== 'cancelled') return true;
  return m.phase_history.some((h) => h.to_phase === 'ready' || h.to_phase === 'blocked');
}

export interface RunningStep {
  /** `#<number> <kind>` like the convergence chart, or the step's name when it is no pass. */
  label: string;
  pass: { number: number; kind: SyncPass['kind'] } | null;
}

/**
 * The step a migration is running, named from its phase and the passes that ended (SDD §4.2, §16): the
 * API lists a pass only once it ends — precopy runs the full copy, syncing the next delta pass, cutover
 * the final pass of a warm migration, the full copy of a cold one or the volume handover.
 */
export function runningStep(m: Pick<Migration, 'phase' | 'strategy' | 'sync_passes'>): RunningStep | null {
  const number = m.sync_passes.reduce((last, p) => (p.ended_at === null ? last : Math.max(last, p.number)), 0) + 1;
  const pass = (kind: SyncPass['kind']): RunningStep => ({ label: `#${number} ${kind}`, pass: { number, kind } });
  switch (m.phase) {
    case 'precopy':
      return pass('full');
    case 'syncing':
      return pass('delta');
    case 'cutover':
      if (isWarmStrategy(m.strategy)) return pass('final');
      return m.strategy === 'storage_handover' ? { label: 'Volume handover', pass: null } : pass('full');
    default:
      return null;
  }
}
