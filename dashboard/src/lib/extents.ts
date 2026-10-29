import type { ExtentUse, SyncPass } from '../api/types';

/** The reasons a pass scanned a disk in full, in words (SDD §28.5). */
const SCAN_REASONS: Record<string, string> = {
  first_pass: 'first pass',
  not_tracked: 'not tracked',
  encrypted: 'encrypted',
  no_base: 'no base',
  base_gone: 'base gone',
  resized: 'resized',
  missed: 'missed earlier',
  unchecked: 'unchecked',
  assumed_zero: 'assumed zero',
  query_failed: 'query failed',
};

function reasonText(reason: string | null): string {
  return reason ? (SCAN_REASONS[reason] ?? reason.replace(/_/g, ' ')) : 'no reason';
}

/** How a pass used a device's extents, in the chart's data table (SDD §16, §28.5, plan task CE10). */
export function extentUseText(device: string, use: ExtentUse): string {
  if (use.mode === 'read') return `${device} read`;
  if (use.mode === 'checked') return use.missed > 0 ? `${device} checked, ${use.missed} missed` : `${device} checked`;
  return `${device} scanned (${reasonText(use.reason)})`;
}

/** A pass's extent use over its devices, in device order; "—" without any. */
export function passExtentsText(pass: SyncPass): string {
  return (
    Object.entries(pass.extents ?? {})
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([device, use]) => extentUseText(device, use))
      .join('; ') || '—'
  );
}

/** Whether any listed pass recorded an extent use: a plan that tracks extents (SDD §28.5). */
export function tracksExtents(passes: SyncPass[]): boolean {
  return passes.some((p) => p.extents && Object.keys(p.extents).length > 0);
}

/** The extent use the latest pass recorded per device, for the Disks list (SDD §16). */
export function latestExtentUse(passes: SyncPass[]): Record<string, ExtentUse> {
  return [...passes].reverse().find((p) => p.extents && Object.keys(p.extents).length > 0)?.extents ?? {};
}

/** A disk's mark from its latest extent use: a miss turns tracking off for it (SDD §28.5). */
export function diskExtentText(use: ExtentUse): string {
  if (use.mode === 'read') return 'Extents read';
  if (use.mode === 'checked') {
    return use.missed > 0 ? `Extents missed ${use.missed} ${use.missed === 1 ? 'chunk' : 'chunks'}: scans in full from now on` : 'Extents checked';
  }
  return `Scanned: ${reasonText(use.reason)}`;
}

/** Whether a disk's latest extent use is the warning: a check found changes outside its extents. */
export function extentsMissed(use: ExtentUse): boolean {
  return use.mode === 'checked' && use.missed > 0;
}
