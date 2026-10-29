import type { LucideIcon } from 'lucide-react';
import type { DerivedPlanStatus, Phase, ProviderStatus, Severity } from '../api/types';
import { cn } from '../lib/cn';
import { NOTHING_MIGRATED } from '../lib/planDone';
import { phaseMeta, type Tone } from '../lib/phase';
import { planStatusMeta, providerStatusMeta, severityMeta, TONE_CLASSES } from '../lib/status';
import { Term } from './Term';

export interface StatusBadgeProps {
  tone: Tone;
  icon: LucideIcon;
  label: string;
  /** Longer explanation shown as a tooltip, for detail the label already conveys (a phase's description). */
  title?: string;
  /**
   * What the status asks for or why — a readiness flag's guidance: the label becomes a term that gives it on hover,
   * focus and tap, never only in a title (SDD §16).
   */
  detail?: string;
  size?: 'sm' | 'md';
  className?: string;
}

/** Status is always icon + text + colour — never colour alone (SDD §16). */
export function StatusBadge({ tone, icon: Icon, label, title, detail, size = 'sm', className }: StatusBadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex min-w-0 max-w-full items-center gap-1 wrap-anywhere whitespace-normal rounded-sm border font-medium',
        size === 'sm' ? 'px-1.5 py-0.5 text-xs' : 'px-2 py-1 text-sm',
        TONE_CLASSES[tone].badge,
        className,
      )}
    >
      <Icon aria-hidden className={cn('shrink-0', size === 'sm' ? 'size-3.5' : 'size-4')} />
      {/* Labels wrap at large text sizes; never hide status behind an ellipsis (SDD §16). */}
      {detail ? <Term definition={detail}>{label}</Term> : <span>{label}</span>}
    </span>
  );
}

export function PhaseBadge({ phase, size, className }: { phase: Phase; size?: 'sm' | 'md'; className?: string }) {
  const meta = phaseMeta(phase);
  return <StatusBadge tone={meta.tone} icon={meta.icon} label={meta.label} title={meta.description} size={size} className={className} />;
}

export function PlanStatusBadge({ status, size, nothingMigrated }: { status: DerivedPlanStatus; size?: 'sm' | 'md'; nothingMigrated?: boolean }) {
  const meta = planStatusMeta(status);
  // a Completed plan that migrated nothing is no success: neutral, with the rolled-back icon and why (SDD §16)
  return nothingMigrated ? (
    <StatusBadge tone="neutral" icon={phaseMeta('rolled_back').icon} label={meta.label} detail={NOTHING_MIGRATED} size={size} />
  ) : (
    <StatusBadge tone={meta.tone} icon={meta.icon} label={meta.label} size={size} />
  );
}

export function ProviderStatusBadge({ status, size }: { status: ProviderStatus; size?: 'sm' | 'md' }) {
  const meta = providerStatusMeta(status);
  return <StatusBadge tone={meta.tone} icon={meta.icon} label={meta.label} size={size} />;
}

export function SeverityBadge({ severity, size }: { severity: Severity; size?: 'sm' | 'md' }) {
  const meta = severityMeta(severity);
  return <StatusBadge tone={meta.tone} icon={meta.icon} label={meta.label} size={size} />;
}
