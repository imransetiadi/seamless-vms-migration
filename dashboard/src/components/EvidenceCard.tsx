import { CircleMinus, CirclePlus, Info, TriangleAlert, type LucideIcon } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { AdvisorRecommendation, EvidenceEffect } from '../api/types';
import { cn } from '../lib/cn';
import { EFFECT_LABELS, evidenceHref, evidenceSummary, shownEvidence, SOURCE_LABELS } from '../lib/evidence';
import { formatPct } from '../lib/format';
import { Button } from './Button';

const EFFECT_ICONS: Record<EvidenceEffect, LucideIcon> = { supports: CirclePlus, opposes: CircleMinus, context: Info };
const EFFECT_TONES: Record<EvidenceEffect, string> = {
  supports: 'text-status-success',
  opposes: 'text-status-danger',
  context: 'text-muted-foreground',
};

/**
 * The evidence of a recommendation (SDD §16, ADV-02): the six heaviest facts, those that support or oppose
 * first, each with its effect as icon and word, its value and a link to its source; the rest behind
 * "Show all"; "Limited evidence" when the decision waited for it; what memory knew; Jev's reasons.
 */
export function EvidenceCard({ rec }: { rec: AdvisorRecommendation }) {
  const [all, setAll] = useState(false);
  const { shown, rest } = shownEvidence(rec.evidence);
  const items = all ? [...shown, ...rest] : shown;
  const limited = rec.decision.reason_code === 'INSUFFICIENT_EVIDENCE';
  const labels = new Map(rec.evidence.map((i) => [i.key, i.label]));
  if (rec.evidence.length === 0 && !limited) {
    return <p className="text-sm text-muted-foreground">No evidence was recorded with this recommendation.</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      {limited && (
        <p role="note" className="flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-2.5 text-sm text-foreground">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          {evidenceSummary(rec)}. The policy in force asks for more before it applies a decision on its own.
        </p>
      )}
      {items.length > 0 && (
        <ul aria-label="Evidence items" className="flex flex-col divide-y divide-border rounded-md border border-border">
          {items.map((item) => {
            const Icon = EFFECT_ICONS[item.effect];
            const href = evidenceHref(item, rec);
            return (
              <li key={item.key} aria-label={item.label} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2 text-sm">
                <span className={cn('inline-flex w-24 shrink-0 items-center gap-1.5 font-medium', EFFECT_TONES[item.effect])}>
                  <Icon aria-hidden className="size-4 shrink-0 self-center" />
                  {EFFECT_LABELS[item.effect]}
                </span>
                <span className="min-w-0 flex-1 wrap-break-word text-foreground">{item.display}</span>
                {href && (
                  <Link to={href} className="text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground">
                    {SOURCE_LABELS[item.source.kind]}
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {rest.length > 0 && (
        <Button size="sm" variant="ghost" className="self-start" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : `Show all (${rec.evidence.length})`}
        </Button>
      )}
      {rec.reasons.length > 0 && (
        <div>
          <h3 className="mb-1 text-sm font-medium text-foreground">What Jev said</h3>
          <ul aria-label="Advisor's reasons" className="flex flex-col gap-1 text-sm">
            {rec.reasons.map((reason) => (
              <li key={`${reason.key}-${reason.text}`} className="text-foreground">
                <span className="text-muted-foreground">{labels.get(reason.key) ?? reason.key}:</span> {reason.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <h3 className="mb-1 text-sm font-medium text-foreground">Similar past incidents</h3>
        {rec.similar_incidents.length === 0 ? (
          <p className="text-sm text-muted-foreground">No similar past incident was known when it was recorded.</p>
        ) : (
          <ul aria-label="Similar past incidents" className="flex flex-col gap-2">
            {rec.similar_incidents.map((incident) => (
              <li key={incident.title} className="text-sm">
                <p className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium text-foreground">{incident.title}</span>
                  {incident.score !== null && <span className="num text-xs text-muted-foreground">match {formatPct(incident.score * 100, 0)}</span>}
                </p>
                <p className="wrap-break-word text-muted-foreground">{incident.content}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
