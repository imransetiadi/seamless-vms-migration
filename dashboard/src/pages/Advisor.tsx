import { Bot, BrainCircuit, CircleCheck, CircleSlash, CircleX, ScrollText, Search, SearchX, SlidersHorizontal } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { useId, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { useAdvisorStatus, useMigrations, usePlans, useRecommendations, useSimilarIncidents } from '../api/hooks';
import { useRole } from '../api/session';
import { ADVISOR_QUERY_MAX, DECISION_TYPES, type DecisionType } from '../api/types';
import { AdvisorNotes, type AdvisorNoteRow } from '../components/AdvisorNotes';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { DataSentPanel } from '../components/DataSentPanel';
import { DeploymentBadge } from '../components/DeploymentBadge';
import { RecommendationActions } from '../components/RecommendationActions';
import { RecommendationList, RecommendationPager } from '../components/Recommendations';
import { LoadingBlock } from '../components/Skeleton';
import { StatusBadge } from '../components/StatusBadge';
import { DECISION_TYPE_LABELS, nonStrategyNotes } from '../lib/advisor';
import { buttonClassName } from '../lib/buttonStyles';
import { formatPct } from '../lib/format';
import { can, hasRole } from '../lib/roles';
import { usePageTitle } from '../lib/usePageTitle';

const SUGGESTIONS = ['kernel panic after import', 'warm sync does not converge', 'volume type not mapped', 'MTU after cutover'];
/** Recommendations per page of the feed. */
const FEED_PAGE = 20;

function ServiceCard({
  title,
  icon: Icon,
  state,
  lines,
  lastError,
  extra,
}: {
  title: string;
  icon: typeof Bot;
  state: 'available' | 'unavailable' | 'off';
  lines: Array<[string, string]>;
  lastError?: string | null;
  /** Beside the availability, e.g. where the model runs. */
  extra?: ReactNode;
}) {
  const badge =
    state === 'available'
      ? { tone: 'success' as const, icon: CircleCheck, label: 'Available' }
      : state === 'off'
        ? { tone: 'neutral' as const, icon: CircleSlash, label: 'Off' }
        : { tone: 'danger' as const, icon: CircleX, label: 'Unavailable' };
  return (
    <section aria-label={title} className="card flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
          <Icon aria-hidden className="size-5 text-muted-foreground" />
          {title}
        </h2>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {extra}
          <StatusBadge tone={badge.tone} icon={badge.icon} label={badge.label} size="md" />
        </div>
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
        {lines.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="text-foreground">{v}</dd>
          </div>
        ))}
      </dl>
      {lastError && (
        <p className="wrap-break-word rounded-md border border-status-warning/40 bg-status-warning/10 px-2.5 py-1.5 text-xs text-foreground">
          Last error: {lastError}
        </p>
      )}
    </section>
  );
}

export default function Advisor() {
  usePageTitle('Advisor');
  const queryId = useId();
  const status = useAdvisorStatus();
  const migrations = useMigrations();
  const search = useSimilarIncidents();
  const role = useRole();
  const canSearch = hasRole(role, 'operator');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState('5');
  const [touched, setTouched] = useState(false);
  const [feedOffset, setFeedOffset] = useState(0);
  // the feed's filters live in the address like the Events plan filter (SDD §16); a change goes back to page one
  const [params, setParams] = useSearchParams();
  const planFilter = params.get('plan') ?? '';
  const typeParam = params.get('type') ?? '';
  const typeFilter = (DECISION_TYPES as readonly string[]).includes(typeParam) ? (typeParam as DecisionType) : '';
  const plans = usePlans();
  const setFilter = (key: 'plan' | 'type', value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setFeedOffset(0);
  };
  const recommendations = useRecommendations(
    { limit: FEED_PAGE, offset: feedOffset, plan_id: planFilter || undefined, decision_type: typeFilter || undefined },
    { keepPrevious: true },
  );

  const s = status.data;
  const memoryEnabled = s?.memory.enabled ?? false;
  // why Search is unavailable: a status still loading or not loaded is not "not enabled" (SDD §16)
  const searchReason = !canSearch
    ? 'Searching requires the operator role.'
    : status.isPending
      ? 'Checking whether agentmemory is enabled…'
      : !s
        ? 'The advisor status could not be loaded.'
        : memoryEnabled
          ? null
          : 'agentmemory is not enabled.';

  // a decision that waits for a person is decided in the feed (SDD §14.6): the held migration knows it
  const byId = useMemo(() => new Map((migrations.data ?? []).map((m) => [m.id, m])), [migrations.data]);
  const [decided, setDecided] = useState('');
  const notes = useMemo<AdvisorNoteRow[]>(
    () =>
      (migrations.data ?? [])
        .flatMap((m) => nonStrategyNotes(m.advisor_notes).map((n) => ({ ...n, vmName: m.vm.name, migrationId: m.id })))
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
        .slice(0, 20),
    [migrations.data],
  );

  const run = (text: string) => {
    setTouched(true);
    if (!text.trim() || !canSearch || !memoryEnabled) return;
    search.mutate({ query: text.trim(), limit: Number(limit) });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    run(query);
  };


  return (
    <>
      <PageHeader
        title="Advisor"
        description="Jev gives bounded, evidence-based judgments (strategy ties, workload tiers, verification review, console screening); agentmemory recalls what happened in past migrations. Both are optional and advisory."
        actions={
          <>
            <Link to="/advisor/policies" className={buttonClassName('secondary', 'md')}>
              <SlidersHorizontal aria-hidden className="size-4 shrink-0" />
              Policies
            </Link>
            <Link to="/advisor/audit" className={buttonClassName('secondary', 'md')}>
              <ScrollText aria-hidden className="size-4 shrink-0" />
              Audit trail
            </Link>
          </>
        }
      />
      <p role="status" aria-live="polite" className="sr-only">
        {decided}
      </p>

      {status.isPending && <LoadingBlock label="Checking advisor services…" rows={2} />}
      {status.error && <ErrorBanner error={status.error} title="Advisor status is unavailable" onRetry={() => void status.refetch()} />}
      {s && (
        <div className="mb-4 grid gap-4 md:grid-cols-2">
          <ServiceCard
            title="Jev"
            icon={Bot}
            state={s.jev.mode === 'off' ? 'off' : s.jev.available ? 'available' : 'unavailable'}
            lines={[
              ['Mode', s.jev.mode],
              ['Used for', 'strategy ties, classification, verification, screening'],
              // the console excerpt leaves only for an on-prem model, or when allowed (SDD §13.4)
              ['Console', s.jev.console_sent ? 'Sent for screening' : 'Stays in this installation'],
            ]}
            extra={<DeploymentBadge deployment={s.jev.deployment} host={s.jev.endpoint_host} />}
            lastError={s.jev.last_error}
          />
          <ServiceCard
            title="agentmemory"
            icon={BrainCircuit}
            state={!s.memory.enabled ? 'off' : s.memory.available ? 'available' : 'unavailable'}
            lines={[
              ['Enabled', s.memory.enabled ? 'Yes' : 'No'],
              ['Used for', 'similar incidents, lessons from completed and failed runs'],
            ]}
            lastError={s.memory.last_error}
          />
        </div>
      )}

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <Panel title="Recommendations" description="What the advisor recommended and what was decided, newest first. Migrations run the decision.">
          {recommendations.isPending ? (
            <LoadingBlock rows={4} />
          ) : recommendations.error ? (
            <ErrorBanner error={recommendations.error} title="Recommendations are unavailable" onRetry={() => void recommendations.refetch()} />
          ) : (
            <>
              <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                <SelectField
                  label="Plan"
                  value={planFilter}
                  onChange={(e) => setFilter('plan', e.target.value)}
                  options={[
                    { value: '', label: 'All plans' },
                    ...(plans.data ?? []).map((p) => ({ value: p.id, label: p.name })),
                    // a plan in the address that the list does not hold stays chosen
                    ...(planFilter && !(plans.data ?? []).some((p) => p.id === planFilter) ? [{ value: planFilter, label: planFilter }] : []),
                  ]}
                />
                <SelectField
                  label="Decision"
                  value={typeFilter}
                  onChange={(e) => setFilter('type', e.target.value)}
                  options={[{ value: '', label: 'All decisions' }, ...DECISION_TYPES.map((t) => ({ value: t, label: DECISION_TYPE_LABELS[t] }))]}
                />
              </div>
              <RecommendationList
                items={recommendations.data.items}
                emptyText={planFilter || typeFilter ? 'No recommendations match these filters.' : 'No recommendations yet.'}
                actionsFor={(rec, focusAfterRef) => {
                  const m = rec.migration_id ? byId.get(rec.migration_id) : undefined;
                  return can(role, 'advisor.approve') && m && m.advisor_hold === rec.id ? (
                    <RecommendationActions rec={rec} migration={m} focusAfterRef={focusAfterRef} onDecided={setDecided} />
                  ) : null;
                }}
              />
              <RecommendationPager offset={recommendations.data.offset} limit={FEED_PAGE} total={recommendations.data.total} onChange={setFeedOffset} />
            </>
          )}
        </Panel>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="Similar incidents" description="Search agentmemory for past failures and their fixes">
            <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
              <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_7rem_auto] sm:items-end">
                <div className="min-w-0">
                  <label htmlFor={queryId} className="field-label">
                    Describe the problem
                  </label>
                  <input
                    id={queryId}
                    type="search"
                    className="input"
                    placeholder="e.g. warm sync does not converge on PostgreSQL"
                    maxLength={ADVISOR_QUERY_MAX}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    aria-invalid={touched && !query.trim() ? true : undefined}
                    aria-describedby={touched && !query.trim() ? `${queryId}-error` : undefined}
                  />
                </div>
                <SelectField
                  label="Results"
                  value={limit}
                  onChange={(e) => setLimit(e.target.value)}
                  options={['3', '5', '10'].map((v) => ({ value: v, label: v }))}
                />
                <Button
                  type="submit"
                  size="lg"
                  variant="primary"
                  icon={Search}
                  loading={search.isPending}
                  disabledReason={searchReason}
                >
                  Search
                </Button>
              </div>
              {touched && !query.trim() && (
                <p id={`${queryId}-error`} role="alert" className="field-error">
                  Enter a few words about the failure to search for.
                </p>
              )}
              {canSearch && memoryEnabled && (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>Try:</span>
                  {SUGGESTIONS.map((text) => (
                    <button
                      key={text}
                      type="button"
                      onClick={() => {
                        setQuery(text);
                        run(text);
                      }}
                      className="min-h-8 cursor-pointer rounded-full border border-border px-2.5 text-foreground transition-colors duration-200 hover:bg-muted"
                    >
                      {text}
                    </button>
                  ))}
                </div>
              )}
            </form>
            <p role="status" className="sr-only">
              {search.data ? `${search.data.hits.length} similar incident${search.data.hits.length === 1 ? '' : 's'} found.` : ''}
            </p>
            <div className="mt-4">
              {search.error && <ErrorBanner error={search.error} title="The search failed" />}
              {search.data && search.data.hits.length === 0 && <EmptyState icon={SearchX} title="No similar incidents found" />}
              {search.data && search.data.hits.length > 0 && (
                <ol aria-label="Similar incidents" className="flex flex-col gap-2">
                  {search.data.hits.map((hit, i) => (
                    <li key={`${hit.title}-${i}`} className="rounded-md border border-border p-3 text-sm">
                      <p className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="font-medium text-foreground">{hit.title}</span>
                        {typeof hit.score === 'number' && <span className="num text-xs text-muted-foreground">match {formatPct(hit.score * 100, 0)}</span>}
                      </p>
                      <p className="mt-1 wrap-break-word text-muted-foreground">{hit.content}</p>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </Panel>

          <Panel title="Recent advisor notes" description="Classification, verification, screening and similar incidents across all migrations, newest first">
            {migrations.isPending ? (
              <LoadingBlock rows={4} />
            ) : migrations.error ? (
              <ErrorBanner error={migrations.error} title="Advisor notes are unavailable" onRetry={() => void migrations.refetch()} />
            ) : (
              <AdvisorNotes notes={notes} emptyText="No advisor notes yet." />
            )}
          </Panel>
        </div>
      </div>
      {/* what each decision type sends, below the feed it explains (SDD §13.4, §16) */}
      <div className="mt-4">
        <DataSentPanel />
      </div>
    </>
  );
}
