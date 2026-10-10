import { CircleCheck, Hourglass, TriangleAlert, type LucideIcon } from 'lucide-react';
import { formatDuration } from './format';

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

/**
 * The label beside a strategy's bar: its average downtime, and whether it is over the SLO - in one piece.
 * Recharts gives a right-hand label its bar's width and wraps it at spaces, so a short bar read "4" above
 * "s"; non-breaking spaces keep it on one line (SDD §16).
 */
export function barLabelText(downtime: number, over: boolean): string {
  return `${formatDuration(downtime)}${over ? ' · over SLO' : ''}`.replace(/ /g, '\u00a0');
}
