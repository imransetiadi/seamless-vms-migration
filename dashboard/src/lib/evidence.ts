import type { AdvisorRecommendation, EvidenceEffect, EvidenceItem, EvidenceSourceKind } from '../api/types';
import { optionLabel } from './advisor';

/** How many items the card shows before "Show all" (SDD §16, ADV-02). */
export const EVIDENCE_SHOWN = 6;

export const EFFECT_LABELS: Record<EvidenceEffect, string> = {
  supports: 'Supports',
  opposes: 'Opposes',
  context: 'Context',
};

/** What a source link opens. */
export const SOURCE_LABELS: Record<EvidenceSourceKind, string> = {
  inventory: 'Inventory',
  preflight: 'Pre-flight',
  estimate: 'Estimates',
  run: 'Earlier run',
  plan: 'Plan',
  provider: 'Providers',
};

const bearing = (item: EvidenceItem) => item.effect !== 'context';

/** The card's order: those that support or oppose first, then by weight; the first six shown. */
export function shownEvidence(items: readonly EvidenceItem[]): { shown: EvidenceItem[]; rest: EvidenceItem[] } {
  const ordered = [...items].sort((a, b) => Number(bearing(b)) - Number(bearing(a)) || b.weight - a.weight);
  return { shown: ordered.slice(0, EVIDENCE_SHOWN), rest: ordered.slice(EVIDENCE_SHOWN) };
}

export function evidenceTally(items: readonly EvidenceItem[]): Record<EvidenceEffect, number> {
  const tally = { supports: 0, opposes: 0, context: 0 };
  for (const item of items) tally[item.effect] += 1;
  return tally;
}

/** Where an item came from: the VM in Inventory, the migration's findings or estimates, the migration, the plan, the providers. */
export function evidenceHref(item: EvidenceItem, rec: Pick<AdvisorRecommendation, 'provider_id'>): string | null {
  const ref = encodeURIComponent(item.source.ref_id);
  switch (item.source.kind) {
    case 'inventory':
      return rec.provider_id ? `/inventory/${encodeURIComponent(rec.provider_id)}?vm=${ref}` : null;
    case 'preflight':
      return `/migrations/${ref}#findings`;
    case 'estimate':
      return `/migrations/${ref}#estimates`;
    case 'run':
      return `/migrations/${ref}`;
    case 'plan':
      return `/plans/${ref}`;
    case 'provider':
      return '/providers';
  }
}

function verb(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The list's one line (SDD §16): what supports and opposes, or that it was too little; null without evidence. */
export function evidenceSummary(rec: Pick<AdvisorRecommendation, 'evidence' | 'decision'>): string | null {
  const tally = evidenceTally(rec.evidence);
  const count = tally.supports + tally.opposes;
  if (rec.decision.reason_code === 'INSUFFICIENT_EVIDENCE') {
    return `Limited evidence: ${verb(count, 'item supports or opposes', 'items support or oppose')}`;
  }
  if (rec.evidence.length === 0) return null;
  return `Evidence: ${verb(tally.supports, 'supports', 'support')}, ${verb(tally.opposes, 'opposes', 'oppose')}`;
}

/** The card's description: "Evidence for {option}" (SDD §16). */
export function evidenceTitle(rec: Pick<AdvisorRecommendation, 'evidence_option'>): string {
  return rec.evidence_option
    ? `Evidence for ${optionLabel(rec.evidence_option)}: what validation knew when it was recorded`
    : 'What validation knew when it was recorded';
}
