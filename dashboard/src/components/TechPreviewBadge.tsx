import { FlaskConical } from 'lucide-react';
import { StatusBadge } from './StatusBadge';

/** What a GPU element's badge says until §22.14's lab cases pass (SDD §16, §22.14). */
export const TECH_PREVIEW_DETAIL = 'Technology Preview, validated in simulation: GPU migration has not yet run on GPU hosts.';
/** What a network cut-over element's badge says until the LAB-NET cases pass (SDD §16, §23.10, plan task N12). */
export const NETWORK_PREVIEW_DETAIL = "Technology Preview, validated in simulation: the network cut-over has not yet run on the lab's clouds.";

/**
 * The "Technology Preview" badge every GPU element (SDD §16, §22.11) and every network cut-over element (§23.9) carries:
 * its label in words beside its icon, never colour alone, and its detail a focusable term, as a status badge's, on
 * hover, focus and tap.
 */
export function TechPreviewBadge({ className, feature = 'gpu' }: { className?: string; feature?: 'gpu' | 'network' }) {
  const detail = feature === 'network' ? NETWORK_PREVIEW_DETAIL : TECH_PREVIEW_DETAIL;
  return <StatusBadge tone="info" icon={FlaskConical} label="Technology Preview" detail={detail} className={className} />;
}
