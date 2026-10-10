import { Bot, ChevronLeft, ChevronRight, History, ListChecks, type LucideIcon } from 'lucide-react';
import { useRef, type ReactNode, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { AdvisorRecommendation } from '../api/types';
import { CONFIDENCE_DEFINITION, DECISION_TYPE_LABELS, decisionText, recommendedParts } from '../lib/advisor';
import { formatDateTime, formatNumber, formatRelative } from '../lib/format';
import { ADVISOR_SOURCE_LABELS } from '../lib/status';
import { Button } from './Button';
import { DeploymentBadge } from './DeploymentBadge';
import { EmptyState } from './EmptyState';
import { Term } from './Term';

const MODEL_ICON: Record<AdvisorRecommendation['model']['name'], LucideIcon> = { jev: Bot, rules: ListChecks };

/** "Recommended: Warm migration · 57% confidence (threshold 60%)": the confidence once, the word explained (ADV-03). */
export function RecommendedLine({ rec, className }: { rec: AdvisorRecommendation; className?: string }) {
  const parts = recommendedParts(rec);
  const answered = rec.recommendation.option !== null;
  return (
    <p className={className}>
      Recommended: {parts.option}
      {answered &&
        (parts.confidence === null ? (
          ' · no confidence given'
        ) : (
          <>
            {' · '}
            {parts.confidence} <Term definition={CONFIDENCE_DEFINITION}>confidence</Term>
          </>
        ))}
      {answered && parts.threshold !== null && ` (threshold ${parts.threshold})`}
    </p>
  );
}

/** "Decision: Cold migration — below threshold, default kept": what the migration runs, and why. */
export function DecisionLine({ rec, className }: { rec: AdvisorRecommendation; className?: string }) {
  return <p className={className}>Decision: {decisionText(rec)}</p>;
}

/** The actions for one entry; `focusAfterRef` is where focus goes after a decision that removes them (SDD §16). */
export type ActionsFor = (rec: AdvisorRecommendation, focusAfterRef: RefObject<HTMLElement>) => ReactNode;

/** What the advisor recommended, then what was decided: the line the migration runs (SDD §16). Untrusted text renders as plain text. */
function RecommendationItem({ rec, showSubject, actionsFor }: { rec: AdvisorRecommendation; showSubject: boolean; actionsFor?: ActionsFor }) {
  const ModelIcon = MODEL_ICON[rec.model.name] ?? Bot;
  // the entry stays when its actions go: focus moves here after a decision (SDD §16)
  const detailsRef = useRef<HTMLAnchorElement>(null);
  return (
    <li className="py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="font-semibold text-foreground">{DECISION_TYPE_LABELS[rec.decision_type] ?? rec.decision_type}</span>
        <span className="inline-flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 text-muted-foreground">
          <ModelIcon aria-hidden className="size-3.5" />
          {ADVISOR_SOURCE_LABELS[rec.model.name] ?? rec.model.name}
        </span>
        {/* where the model that answered runs (SDD §13.4, §16) */}
        {rec.model.name === 'jev' && <DeploymentBadge deployment={rec.model.deployment} host={rec.model.endpoint_host} />}
        {rec.legacy && (
          <span className="inline-flex items-center gap-1 rounded-sm border border-border bg-muted px-1.5 py-0.5 text-muted-foreground">
            <History aria-hidden className="size-3.5" />
            Legacy
          </span>
        )}
        {showSubject &&
          (rec.migration_id ? (
            <Link to={`/migrations/${rec.migration_id}`} className="font-mono underline underline-offset-4">
              {rec.subject.name}
            </Link>
          ) : (
            <span className="font-mono">{rec.subject.name}</span>
          ))}
        <time dateTime={rec.created_at} title={formatDateTime(rec.created_at)} className="text-muted-foreground">
          {formatRelative(rec.created_at)}
        </time>
        <Link ref={detailsRef} to={`/advisor/recommendations/${encodeURIComponent(rec.id)}`} className="ml-auto underline underline-offset-4">
          Details<span className="sr-only"> for {rec.subject.name}</span>
        </Link>
      </div>
      <RecommendedLine rec={rec} className="mt-1 wrap-break-word text-sm text-muted-foreground" />
      <DecisionLine rec={rec} className="mt-0.5 wrap-break-word text-sm font-medium text-foreground" />
      {rec.decision.reason_text && <p className="mt-0.5 wrap-break-word text-xs text-muted-foreground">{rec.decision.reason_text}</p>}
      {rec.legacy && rec.legacy_text && (
        <p className="mt-1.5 wrap-break-word border-l-2 border-border pl-2 text-xs text-muted-foreground">Original note: {rec.legacy_text}</p>
      )}
      {actionsFor?.(rec, detailsRef)}
    </li>
  );
}

/** Advisor recommendations, newest first as the API lists them (SDD §14.4). */
export function RecommendationList({
  items,
  showSubject = true,
  emptyText = 'No recommendations yet.',
  actionsFor,
}: {
  items: AdvisorRecommendation[];
  /** Name and link the VM of each entry (off on the migration's own page). */
  showSubject?: boolean;
  emptyText?: string;
  /** Approve and Override for an entry a person may decide on (SDD §14.6), else nothing. */
  actionsFor?: ActionsFor;
}) {
  if (items.length === 0) return <EmptyState icon={Bot} title={emptyText} />;
  return (
    <ul className="-my-2 divide-y divide-border">
      {items.map((rec) => (
        <RecommendationItem key={rec.id} rec={rec} showSubject={showSubject} actionsFor={actionsFor} />
      ))}
    </ul>
  );
}

/** Previous / next over a page of `GET /advisor/recommendations` (SDD §12). */
export function RecommendationPager({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (offset: number) => void }) {
  if (total <= limit && offset === 0) return null;
  const first = Math.min(offset + 1, total);
  const last = Math.min(offset + limit, total);
  return (
    <nav aria-label="Recommendation pages" className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
      <span className="num text-xs text-muted-foreground">
        {formatNumber(first)}–{formatNumber(last)} of {formatNumber(total)}
      </span>
      <div className="flex gap-2">
        <Button size="sm" icon={ChevronLeft} disabledReason={offset === 0 ? 'This is the first page.' : null} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        <Button size="sm" icon={ChevronRight} disabledReason={last >= total ? 'This is the last page.' : null} onClick={() => onChange(offset + limit)}>
          Next
        </Button>
      </div>
    </nav>
  );
}
