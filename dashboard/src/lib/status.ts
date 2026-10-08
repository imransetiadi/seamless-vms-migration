import type { LucideIcon } from 'lucide-react';
import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  CircleX,
  Info,
  ListChecks,
  OctagonAlert,
  Pause,
  PencilLine,
  Play,
  TriangleAlert,
} from 'lucide-react';
import type { AdvisorNoteKind, AdvisorNoteSource, PlanStatus, ProviderKind, ProviderStatus, Severity, Strategy } from '../api/types';
import type { Tone } from './phase';

export interface StatusMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
}

/**
 * Literal class strings per tone (Tailwind's JIT only sees complete class names).
 * Status text uses the `status-*` tokens, which meet 4.5:1 on card, muted and tinted surfaces.
 */
export const TONE_CLASSES: Record<Tone, { text: string; badge: string; bar: string; border: string }> = {
  neutral: {
    text: 'text-status-neutral',
    badge: 'border-status-neutral/40 bg-status-neutral/10 text-status-neutral',
    bar: 'bg-status-neutral',
    border: 'border-status-neutral',
  },
  info: {
    text: 'text-status-info',
    badge: 'border-status-info/40 bg-status-info/10 text-status-info',
    bar: 'bg-status-info',
    border: 'border-status-info',
  },
  progress: {
    text: 'text-status-progress',
    badge: 'border-status-progress/40 bg-status-progress/10 text-status-progress',
    bar: 'bg-status-progress',
    border: 'border-status-progress',
  },
  warning: {
    text: 'text-status-warning',
    badge: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
    bar: 'bg-status-warning',
    border: 'border-status-warning',
  },
  success: {
    text: 'text-status-success',
    badge: 'border-status-success/40 bg-status-success/10 text-status-success',
    bar: 'bg-status-success',
    border: 'border-status-success',
  },
  danger: {
    text: 'text-status-danger',
    badge: 'border-status-danger/40 bg-status-danger/10 text-status-danger',
    bar: 'bg-status-danger',
    border: 'border-status-danger',
  },
};

const PLAN_STATUS: Record<PlanStatus, StatusMeta> = {
  draft: { label: 'Draft', tone: 'neutral', icon: PencilLine },
  validated: { label: 'Validated', tone: 'info', icon: ListChecks },
  running: { label: 'Running', tone: 'progress', icon: Play },
  paused: { label: 'Paused', tone: 'warning', icon: Pause },
  completed: { label: 'Completed', tone: 'success', icon: CircleCheck },
  failed: { label: 'Failed', tone: 'danger', icon: CircleX },
};

const PROVIDER_STATUS: Record<ProviderStatus, StatusMeta> = {
  unknown: { label: 'Not checked', tone: 'neutral', icon: CircleHelp },
  ok: { label: 'Healthy', tone: 'success', icon: CircleCheck },
  degraded: { label: 'Degraded', tone: 'warning', icon: TriangleAlert },
  error: { label: 'Error', tone: 'danger', icon: CircleX },
};

const SEVERITY: Record<Severity, StatusMeta> = {
  blocker: { label: 'Blocker', tone: 'danger', icon: OctagonAlert },
  warning: { label: 'Warning', tone: 'warning', icon: TriangleAlert },
  info: { label: 'Info', tone: 'info', icon: Info },
};

const FALLBACK: StatusMeta = { label: 'Unknown', tone: 'neutral', icon: CircleAlert };

export function planStatusMeta(status: PlanStatus): StatusMeta {
  return PLAN_STATUS[status] ?? { ...FALLBACK, label: String(status) };
}

export function providerStatusMeta(status: ProviderStatus): StatusMeta {
  return PROVIDER_STATUS[status] ?? { ...FALLBACK, label: String(status) };
}

export function severityMeta(severity: Severity): StatusMeta {
  return SEVERITY[severity] ?? { ...FALLBACK, label: String(severity) };
}

export const SEVERITY_ORDER: Record<Severity, number> = { blocker: 0, warning: 1, info: 2 };

export const STRATEGY_LABELS: Record<Strategy, string> = {
  cold: 'Cold',
  warm: 'Warm',
  storage_handover: 'Storage handover',
  vmware_cold: 'VMware cold',
  vmware_warm: 'VMware warm',
};

export const STRATEGY_DESCRIPTIONS: Record<Strategy, string> = {
  cold: 'Stop, copy everything over NBD/SSH, boot on RHOSO. Downtime grows with used data.',
  warm: 'Snapshot pre-copy while running, then a final delta sync after shutdown.',
  storage_handover: 'Shared Ceph: Cinder unmanage at the source, manage at RHOSO. Metadata only.',
  vmware_cold: 'vmware-migration-kit full copy plus virt-v2v conversion.',
  vmware_warm: 'vmware-migration-kit CBT passes, then cutover with in-place conversion.',
};

export function strategyLabel(strategy: Strategy | 'auto'): string {
  return strategy === 'auto' ? 'Auto' : (STRATEGY_LABELS[strategy] ?? String(strategy));
}

export const PROVIDER_KIND_LABELS: Record<ProviderKind, string> = {
  openstack: 'OpenStack',
  vmware: 'VMware vSphere',
  rhoso: 'RHOSO 18.0',
};

export const ADVISOR_KIND_LABELS: Record<AdvisorNoteKind, string> = {
  strategy: 'Strategy recommendation',
  classification: 'Workload classification',
  verification: 'Verification review',
  similar_incidents: 'Similar incidents',
  screen: 'Console screening',
};

export const ADVISOR_SOURCE_LABELS: Record<AdvisorNoteSource, string> = {
  jev: 'Jev',
  rules: 'Rules',
  memory: 'agentmemory',
};
