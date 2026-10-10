import { CircleCheck, Hourglass, TriangleAlert, type LucideIcon } from 'lucide-react';

/** Share of the downtime SLO from which a running clock warns (SDD §16). */
export const SLO_WARNING_SHARE = 0.8;

export type SloTone = 'success' | 'warning' | 'danger';

/**
 * One rule for every downtime clock against its SLO (SDD §16): calm below 80 % of the SLO, a
 * warning from 80 %, a breach past 100 % — Overview and the migration page never disagree.
 */
export function sloTone(elapsedS: number, sloS: number): SloTone {
  if (elapsedS > sloS) return 'danger';
  if (elapsedS >= sloS * SLO_WARNING_SHARE) return 'warning';
  return 'success';
}

/** The icon beside each tone, so a tone is never colour alone (SDD §16). */
export const SLO_ICONS: Record<SloTone, LucideIcon> = { success: CircleCheck, warning: Hourglass, danger: TriangleAlert };
