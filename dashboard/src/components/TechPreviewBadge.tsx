import { FlaskConical } from 'lucide-react';
import { StatusBadge } from './StatusBadge';

/** What a GPU element's badge says until §22.14's lab cases pass (SDD §16, §22.14). */
export const TECH_PREVIEW_DETAIL = 'Technology Preview, validated in simulation: GPU migration has not yet run on GPU hosts.';

/**
 * The "Technology Preview" badge every GPU element carries (SDD §16, §22.11): its label in words beside its icon, never
 * colour alone, and its detail a focusable term, as a status badge's, on hover, focus and tap.
 */
export function TechPreviewBadge({ className }: { className?: string }) {
  return <StatusBadge tone="info" icon={FlaskConical} label="Technology Preview" detail={TECH_PREVIEW_DETAIL} className={className} />;
}
