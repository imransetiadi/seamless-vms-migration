import { Hourglass } from 'lucide-react';
import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useRecommendations } from '../api/hooks';
import { useRole } from '../api/session';
import { RECOMMENDATION_LIMIT_MAX, type MigrationLike } from '../api/types';
import { waitingDecision } from '../lib/advisor';
import { can } from '../lib/roles';
import { ErrorBanner } from './ErrorBanner';
import { Panel } from './Panel';
import { RecommendationActions } from './RecommendationActions';
import { DecisionLine, RecommendedLine } from './Recommendations';
import { LoadingBlock } from './Skeleton';

/**
 * The plan's migrations whose advisor decision waits for a person (SDD §14.6); nothing when none does. After the
 * last decision here the panel stays, saying so, and its heading takes the focus (SDD §16).
 */
export function AdvisorApprovals({ planId, migrations, onDecided }: { planId: string; migrations: readonly MigrationLike[]; onDecided?: (message: string) => void }) {
  const held = migrations.filter((m) => waitingDecision(m));
  const recommendations = useRecommendations({ plan_id: planId, limit: RECOMMENDATION_LIMIT_MAX }, { enabled: held.length > 0 });
  const role = useRole();
  const headingRef = useRef<HTMLSpanElement>(null);
  const [decidedHere, setDecidedHere] = useState(false);
  if (held.length === 0 && !decidedHere) return null;
  const decided = (message: string) => {
    setDecidedHere(true);
    onDecided?.(message);
  };
  return (
    <Panel
      title={
        <span ref={headingRef} tabIndex={-1} className="outline-hidden">
          Waiting for advisor approval
        </span>
      }
      description="These migrations start once a person approves or overrides the advisor's decision; nothing applies it on its own."
    >
      {held.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing waits for approval any more.</p>
      ) : recommendations.isPending ? (
        <LoadingBlock rows={2} />
      ) : recommendations.error ? (
        <ErrorBanner error={recommendations.error} title="The waiting recommendations could not be loaded" onRetry={() => void recommendations.refetch()} />
      ) : (
        <ul className="-my-2 divide-y divide-border">
          {held.map((m) => {
            const rec = recommendations.data.items.find((r) => r.id === m.advisor_hold);
            return (
              <li key={m.id} className="py-2.5">
                <p className="flex flex-wrap items-center gap-2 text-xs">
                  <Hourglass aria-hidden className="size-3.5 text-status-warning" />
                  <Link to={`/migrations/${m.id}`} className="font-mono underline underline-offset-4">
                    {m.vm.name}
                  </Link>
                  {rec && (
                    <Link to={`/advisor/recommendations/${encodeURIComponent(rec.id)}`} className="ml-auto underline underline-offset-4">
                      Details<span className="sr-only"> for {m.vm.name}</span>
                    </Link>
                  )}
                </p>
                {rec ? (
                  <>
                    <RecommendedLine rec={rec} className="mt-1 text-sm text-muted-foreground" />
                    <DecisionLine rec={rec} className="mt-0.5 text-sm font-medium text-foreground" />
                    {can(role, 'advisor.approve') && <RecommendationActions rec={rec} migration={m} focusAfterRef={headingRef} onDecided={decided} />}
                  </>
                ) : (
                  <p className="mt-1 text-sm text-muted-foreground">Its recommendation is not among the newest {RECOMMENDATION_LIMIT_MAX} of this plan.</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
