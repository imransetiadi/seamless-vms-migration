import {
  Activity,
  ArrowRight,
  ChartNoAxesCombined,
  ChevronRight,
  CircleCheck,
  CircleHelp,
  CircleX,
  ClipboardList,
  Search,
  SearchX,
  ShieldCheck,
  Timer,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useDemoSpeed, useMigrationSummaries, usePlans, useStats } from '../api/hooks';
import { PHASES, type MigrationSummary, type Phase, type Plan } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { DowntimeSloChart } from '../components/DowntimeSloChart';
import { SelectField } from '../components/Field';
import { MetricStrip } from '../components/MetricStrip';
import { PendingApprovals } from '../components/PendingApprovals';
import { MigrationFinder } from '../components/MigrationFinder';
import { Panel } from '../components/Panel';
import { PhaseDistribution } from '../components/PhaseDistribution';
import { ProgressBar } from '../components/ProgressBar';
import { LoadingBlock, Skeleton } from '../components/Skeleton';
import { PhaseBadge, PlanStatusBadge } from '../components/StatusBadge';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { ThroughputChart } from '../components/ThroughputChart';
import { Unavailable } from '../components/Unavailable';
import { pendingApprovals } from '../lib/approvals';
import { useHashLanding } from '../lib/useHashLanding';
import { attentionItems, type AttentionItem } from '../lib/attention';
import { cn } from '../lib/cn';
import { buttonClassName } from '../lib/buttonStyles';
import { phaseMeta } from '../lib/phase';
import { SLO_ICONS, sloTone } from '../lib/slo';
import { DASH, formatBytes, formatClock, formatDuration, formatNumber, formatPct, secondsBetween } from '../lib/format';
import { demoTimeText, leftOutText, speedOf } from '../lib/stats';
import { strategyLabel, TONE_CLASSES } from '../lib/status';
import { nothingMigrated, phaseCounts, planDone } from '../lib/planDone';
import { usePageTitle } from '../lib/usePageTitle';
import { useNow } from '../lib/useNow';
import { DESTINATIONS } from '../lib/platforms';

const ATTENTION_ICONS: Record<'danger' | 'warning' | 'info', LucideIcon> = {
  danger: CircleX,
  warning: TriangleAlert,
  info: Activity,
};

const BAR_CLASSES: Record<'success' | 'warning' | 'danger', string> = {
  success: 'bg-status-success',
  warning: 'bg-status-warning',
  danger: 'bg-status-danger',
};

const NO_MIGRATIONS: MigrationSummary[] = [];

function QueueShortcut({ id, label, count, loading, icon: Icon, tone }: {
  id: string;
  label: string;
  count: number | null;
  loading: boolean;
  icon: LucideIcon;
  tone: 'info' | 'warning' | 'danger';
}) {
  return (
    <a href={`#${id}`} className="group grid min-w-0 grid-cols-[auto_minmax(0,1fr)] content-start items-center gap-2 rounded-lg border border-border bg-card p-2.5 transition-colors hover:border-ring sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:gap-x-3 sm:gap-y-1 sm:px-4 sm:py-3">
      <span className={cn('col-start-1 row-start-1 flex size-7 items-center justify-center rounded-md sm:row-span-2 sm:size-10', count ? TONE_CLASSES[tone].badge : 'bg-muted text-muted-foreground')}>
        <Icon aria-hidden className="size-4 sm:size-5" />
      </span>
      <span className="col-span-2 row-start-2 wrap-anywhere text-xs font-medium text-muted-foreground sm:col-span-1 sm:col-start-2 sm:row-start-1 sm:text-sm">{label}</span>
      {count === null ? (
        <span className="col-span-2 row-start-3 text-xs font-semibold sm:col-span-1 sm:col-start-2 sm:row-start-2">{loading ? 'Loading…' : 'Unknown'}</span>
      ) : (
        <span className="num col-start-2 row-start-1 min-w-0 wrap-anywhere text-right text-2xl sm:text-left font-semibold leading-none tracking-tight sm:row-start-2 sm:text-3xl">{formatNumber(count)}</span>
      )}
      <ChevronRight aria-hidden className="col-start-3 row-span-2 row-start-1 hidden size-4 text-muted-foreground group-hover:text-foreground sm:block" />
    </a>
  );
}

/**
 * Down right now: in the cutover window (the stop may not be reported yet) or with an open downtime
 * clock — a failed cutover or a rollback keeps the source stopped until it runs again (SDD §5.2).
 */
function isDown(m: MigrationSummary): boolean {
  return m.phase === 'cutover' || m.phase === 'verifying' || (Boolean(m.downtime_started_at) && !m.downtime_ended_at);
}

/** What the VM is waiting for while it is down, after the strategy and plan. */
const DOWN_DETAIL: Partial<Record<Phase, string>> = {
  verifying: ', checking the boot on the destination',
  rolling_back: ', rolling back, restarting the source VM',
  failed: ', failed with the source stopped: roll back or retry',
};

/**
 * The page's centre: every VM whose source is stopped right now, with its downtime clock and how
 * much of the plan's downtime SLO it has used (SDD §7.2 downtime clock, §5.4).
 */
function DowntimeNow({ active, waiting, plans, plansUnknown }: {
  active: readonly MigrationSummary[];
  waiting: number;
  plans: ReadonlyMap<string, Plan>;
  plansUnknown: boolean;
}) {
  // in demo mode the clocks run in model time, as on the migration page (SDD §16, ADV-16)
  const speed = speedOf(useDemoSpeed());
  const now = useNow(speed > 1 ? 250 : 1000, active.length > 0);
  const demoNote = demoTimeText(speed);

  return (
    <Panel
      id="downtime"
      title="Downtime now"
      className="border-t-2 border-t-status-info shadow-sm"
      description={
        active.length
          ? `${active.length} VM${active.length === 1 ? ' is' : 's are'} down. Clocks stop on verified boot at the destination or source restart.${demoNote ? ` ${demoNote}.` : ''}`
          : 'VMs whose source is stopped appear here with a running clock.'
      }
    >
      {active.length === 0 ? (
        <div className="flex flex-col items-start gap-1 py-2">
          <p className="text-base font-medium text-foreground">No VM is down right now.</p>
          <p className="text-sm text-muted-foreground">
            {waiting > 0
              ? `${waiting} migration${waiting === 1 ? ' has' : 's have'} converged and wait${waiting === 1 ? 's' : ''} for the cutover.`
              : 'Cutovers start once approved, inside their window.'}
          </p>
        </div>
      ) : (
        <ul className="-my-1 divide-y divide-border">
          {active.map((m) => {
            const plan = plans.get(m.plan_id);
            const slo = plan?.downtime_slo_s ?? null;
            const elapsed = (secondsBetween(m.downtime_started_at, now) ?? 0) * speed;
            // the migration page's rule, with an icon beside the warning and the breach (SDD §16)
            const tone = slo ? sloTone(elapsed, slo) : 'success';
            const ToneIcon = SLO_ICONS[tone];
            const scale = slo ? Math.max(slo, elapsed) : 1;
            const left = slo !== null ? slo - elapsed : null;
            return (
              <li key={m.id} className="flex flex-col gap-2.5 py-3">
                <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link to={`/migrations/${m.id}`} className="inline-flex min-h-6 min-w-0 items-center gap-2 rounded-md font-mono text-base font-semibold text-foreground underline-offset-4 hover:underline">
                        {m.vm.name}
                        <ArrowRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                      </Link>
                      <PhaseBadge phase={m.phase} />
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {strategyLabel(m.strategy)}
                      {plan ? ` in ${plan.name}` : ''}
                      {m.phase === 'cutover' ? `, final copy ${formatPct(m.progress_pct)}` : (DOWN_DETAIL[m.phase] ?? ', source VM stopped')}
                    </p>
                  </div>
                  <p className="flex max-w-full flex-wrap items-baseline gap-2">
                    <span className="sr-only">Downtime </span>
                    <span className={cn('num min-w-0 wrap-anywhere text-4xl font-semibold leading-none tracking-tight', tone === 'success' ? 'text-foreground' : TONE_CLASSES[tone].text)}>
                      {formatClock(elapsed)}
                    </span>
                    {slo !== null && <span className="num text-sm text-muted-foreground">of {formatClock(slo)}</span>}
                  </p>
                </div>
                {slo === null && plansUnknown && (
                  // a failed load never reads as a met SLO (SDD §16)
                  <p className="flex items-center gap-1 text-xs text-muted-foreground">
                    <CircleHelp aria-hidden className="size-3.5" />
                    SLO unknown: the plans could not be loaded.
                  </p>
                )}
                {slo !== null && (
                  <div className="flex flex-col gap-1">
                    <div
                      role="meter"
                      aria-label={`${m.vm.name} downtime against the SLO`}
                      aria-valuemin={0}
                      aria-valuemax={slo}
                      aria-valuenow={Math.min(elapsed, slo)}
                      aria-valuetext={`${formatClock(elapsed)} of ${formatClock(slo)}`}
                      className="relative h-2 overflow-hidden rounded-full bg-muted"
                    >
                      <div
                        className={cn('h-full rounded-full transition-[width] duration-700 ease-linear motion-reduce:transition-none', BAR_CLASSES[tone])}
                        style={{ width: `${Math.min(100, (elapsed / scale) * 100)}%` }}
                      />
                      {elapsed > slo && (
                        <span aria-hidden className="absolute inset-y-0 w-0.5 bg-foreground" style={{ left: `${(slo / scale) * 100}%` }} />
                      )}
                    </div>
                    <p className={cn('flex items-center gap-1 text-xs', tone === 'success' ? 'text-muted-foreground' : TONE_CLASSES[tone].text)}>
                      {tone !== 'success' && <ToneIcon aria-hidden className="size-3.5" />}
                      {/* the countdown reads as a clock, the SLO as a duration, like the migration page (SDD §16) */}
                      {left !== null && left >= 0
                        ? `${formatClock(left)} left in the ${formatDuration(slo)} SLO`
                        : `Over the SLO by ${formatClock(-(left ?? 0))}`}
                    </p>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function NeedsAttention({ items, plansUnknown }: { items: AttentionItem[]; plansUnknown: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? items : items.slice(0, 8);
  return (
    <Panel id="attention" title="Needs attention" description="The next human action, most urgent first">
      {/* without the plans, approvals and cutover requests cannot be told: never "nothing" (SDD §16) */}
      {plansUnknown && <Unavailable what="plans" className={items.length ? 'mb-2' : undefined} />}
      {items.length === 0 ? (
        !plansUnknown && <EmptyState icon={CircleCheck} title="Nothing needs attention" description="No failures, reviews, cutover requests or blockers are waiting; approvals are in Pending approvals." />
      ) : (
        <>
          <ul className="-my-2 divide-y divide-border">
            {shown.map((item) => {
              const Icon = ATTENTION_ICONS[item.tone];
              return (
                <li key={item.migration.id} className="group relative flex gap-3 rounded-md py-2.5 hover:bg-muted/60 focus-within:bg-muted/60">
                  <Icon aria-hidden className={cn('mt-0.5 size-4 shrink-0', TONE_CLASSES[item.tone].text)} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        to={`/migrations/${item.migration.id}`}
                        className="font-mono text-sm font-semibold text-foreground underline-offset-4 after:absolute after:inset-0 after:rounded-md group-hover:underline"
                      >
                        {item.migration.vm.name}
                      </Link>
                      <PhaseBadge phase={item.migration.phase} />
                      {item.techPreview && <TechPreviewBadge className="relative" />}
                    </div>
                    <p className="mt-0.5 wrap-break-word text-sm text-foreground">{item.reason}</p>
                    <p className="text-xs text-muted-foreground">Next: {item.nextAction}</p>
                  </div>
                </li>
              );
            })}
          </ul>
          {items.length > 8 && (
            <Button className="mt-3" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
              {expanded ? 'Show fewer' : `Show all ${items.length} migrations`}
            </Button>
          )}
        </>
      )}
    </Panel>
  );
}

/** Completion per plan; `migrations` is null when they could not be loaded (progress unknown, SDD §16). */
function PlanProgress({ plans, migrations }: { plans: Plan[]; migrations: MigrationSummary[] | null }) {
  if (plans.length === 0) {
    return <EmptyState icon={ClipboardList} title="No plans yet" description="Create a plan to start migrating." />;
  }
  return (
    // -mt-px hides the top rule of the first row in every column
    <div className="-my-2 overflow-hidden">
    <ul className="-mt-px grid gap-x-8 md:grid-cols-2 2xl:grid-cols-3">
      {plans.map((plan) => {
        // counted as the plans list counts it (SDD §16): a cancelled migration is never done, a cancelled plan has nothing left
        const pr = migrations ? planDone(plan, phaseCounts(migrations.filter((m) => m.plan_id === plan.id))) : undefined;
        const pct = pr?.total ? (pr.done / pr.total) * 100 : 0;
        return (
          <li key={plan.id} className="flex min-w-0 flex-col gap-1.5 border-t border-border py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Link to={`/plans/${plan.id}`} className="min-w-0 wrap-break-word font-medium text-foreground underline-offset-4 hover:underline">
                {plan.name}
              </Link>
              <PlanStatusBadge status={plan.derived_status} nothingMigrated={nothingMigrated(plan)} />
            </div>
            {pr && <ProgressBar value={pct} label={`${plan.name}: migrations done`} tone="success" size="sm" />}
            <p className="num text-xs text-muted-foreground">
              {pr === undefined ? (
                'Progress unknown'
              ) : pr === null ? (
                <>
                  <span aria-hidden>—</span>
                  <span className="sr-only">Nothing left to do</span>
                </>
              ) : (
                // the cancelled ones the count leaves out, named as the Plans list does (SDD §16)
                `${pr.done} of ${pr.total} done${pr.cancelled ? `, ${pr.cancelled} cancelled` : ''}`
              )}
              ,{' '}
              <span>downtime SLO {formatDuration(plan.downtime_slo_s)}</span>
            </p>
          </li>
        );
      })}
    </ul>
    </div>
  );
}

export default function Overview() {
  usePageTitle('Overview');
  const [params, setParams] = useSearchParams();
  const planId = params.get('plan') ?? '';
  const location = useLocation();
  const navigate = useNavigate();
  const from = location.state?.finderPhase;
  const finderPhase: Phase | 'all' | null = from === 'all' || PHASES.includes(from) ? from : null;
  const findButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement>(null);
  const plansQuery = usePlans();
  const stats = useStats(planId || null);
  // summaries: what the page's rules read, not every migration whole (SDD §16, perf audit PF-02)
  const migrationsQuery = useMigrationSummaries(planId ? { plan_id: planId } : {});

  const plans = useMemo(() => plansQuery.data ?? [], [plansQuery.data]);
  const plansById = useMemo(() => new Map(plans.map((p) => [p.id, p])), [plans]);
  const migrations = migrationsQuery.data ?? NO_MIGRATIONS;
  const scopedPlans = planId ? plans.filter((p) => p.id === planId) : plans;
  // a draft has not started and a cancelled plan never will (SDD §12): neither sets the SLO the chart reads
  const activePlans = scopedPlans.filter((p) => p.derived_status !== 'draft' && p.derived_status !== 'cancelled');
  const sloS = activePlans.length ? Math.min(...activePlans.map((p) => p.downtime_slo_s)) : null;
  const sloLabel = planId || activePlans.length <= 1 ? 'SLO' : 'Strictest SLO';
  const s = stats.data;
  // a cancelled migration is never done, as each plan's done count says: Migrations counts the others (SDD §16)
  const cancelled = s?.by_phase.cancelled ?? 0;
  const counted = s ? s.total - cancelled : 0;
  // a failed load with nothing cached is unknown, never an empty state or a zero (SDD §16)
  const plansUnknown = Boolean(plansQuery.error) && !plansQuery.data;
  const statsUnknown = Boolean(stats.error) && !s;
  const migrationsUnknown = Boolean(migrationsQuery.error) && !migrationsQuery.data;
  // a plan id in the address that the loaded plans do not hold (mistyped, or an old link)
  const unknownPlan = Boolean(planId) && Boolean(plansQuery.data) && !plansById.has(planId);

  const now = useNow(60_000);
  const attention = useMemo(() => attentionItems(migrations, plansById, now, { plansUnknown }), [migrations, plansById, plansUnknown, now]);
  const approvals = useMemo(() => pendingApprovals(migrations, plansById, { plansUnknown }), [migrations, plansById, plansUnknown]);
  const down = useMemo(() => migrations.filter(isDown).sort((a, b) =>
    Date.parse(a.downtime_started_at ?? a.updated_at) - Date.parse(b.downtime_started_at ?? b.updated_at),
  ), [migrations]);
  const waiting = down.length ? 0 : migrations.filter((m) => m.phase === 'awaiting_cutover').length;
  const queuesLoading = migrationsQuery.isPending || plansQuery.isPending;
  const countsKnown = !queuesLoading && !plansUnknown && !migrationsUnknown;
  useHashLanding(!queuesLoading && !unknownPlan);

  const setPlan = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set('plan', value);
    else next.delete('plan');
    setParams(next, { replace: true });
  };

  // Replacing this entry gives even a native #queue jump its own router key. The finder keeps
  // its search and pagination in memory for that entry when a result leads to another page.
  const setFinderPhase = (phase: Phase | 'all' | null) => {
    const { pathname, search, hash, state } = location;
    void navigate({ pathname, search, hash }, { replace: true, state: { ...state, finderPhase: phase }, preventScrollReset: true });
  };
  const closeFinder = () => {
    // Live updates may have removed the metric that opened the finder.
    returnFocus.current = document.getElementById(`overview-metric-${finderPhase}`) ?? findButton.current;
    setFinderPhase(null);
  };

  const phaseMetric = (phase: Phase, count: number) => {
    const value = formatNumber(count);
    if (!count || !migrationsQuery.data || stats.isPlaceholderData) return { value };
    const action = phase === 'completed' ? 'View migrations awaiting finalize' : `View ${phaseMeta(phase).label.toLowerCase()} migrations`;
    return { value, inspect: {
      label: `${action} (${value})`,
      id: `overview-metric-${phase}`,
      onClick: () => setFinderPhase(phase),
    } };
  };

  const header = (
    <>
      <header className="mb-4 rounded-xl border border-border bg-linear-to-br from-status-info/10 via-card to-accent/5 p-3 sm:p-5">
        <div className="flex flex-wrap items-end justify-between gap-x-5 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight sm:text-3xl">Overview</h1>
            <p className="mt-1 text-sm text-muted-foreground">Live state of VM migrations into {DESTINATIONS}.</p>
          </div>
          <div className="flex w-full min-w-0 flex-wrap items-end gap-2 sm:w-auto">
            <SelectField
              label="Plan"
              className="min-w-[min(8rem,100%)] flex-1 sm:w-64"
              value={planId}
              onChange={(e) => setPlan(e.target.value)}
              options={[
                { value: '', label: 'All plans' },
                ...plans.map((p) => ({ value: p.id, label: p.name })),
                // a plan in the address that the list does not hold stays chosen (SDD §16), as on Events
                ...(planId && !plansById.has(planId) ? [{ value: planId, label: planId }] : []),
              ]}
            />
            {!unknownPlan && (
              <Button ref={findButton} icon={Search} size="lg" variant="primary" aria-haspopup="dialog"
                disabledReason={migrationsQuery.isPending ? 'Loading migrations…' : migrationsUnknown ? 'Retry the migration list to search.' : null}
                onClick={() => setFinderPhase('all')}>
                Find migration
              </Button>
            )}
          </div>
        </div>
        {!unknownPlan && (
          <nav aria-label="Migration queues" className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(min(100%,5.3rem),1fr))] gap-1.5 sm:grid-cols-3 sm:gap-3">
            <QueueShortcut id="downtime" label="VMs down" count={migrationsQuery.data ? down.length : null} loading={migrationsQuery.isPending} icon={Timer} tone="info" />
            <QueueShortcut id="attention" label="Needs attention" count={countsKnown ? attention.length : null} loading={queuesLoading} icon={TriangleAlert} tone={attention[0]?.tone ?? 'info'} />
            <QueueShortcut id="approvals" label="Pending approvals" count={countsKnown ? approvals.items.length : null} loading={queuesLoading} icon={ShieldCheck} tone="warning" />
          </nav>
        )}
      </header>
      {finderPhase && !unknownPlan && migrationsQuery.data && (
        <MigrationFinder
          migrations={migrations}
          plans={plansById}
          scope={planId ? plansById.get(planId)?.name ?? planId : 'All plans'}
          initialPhase={finderPhase}
          returnFocusRef={returnFocus}
          onClose={closeFinder}
        />
      )}

      {plansQuery.error && <ErrorBanner error={plansQuery.error} title="Plans are unavailable" onRetry={() => void plansQuery.refetch()} className="mb-4" />}
    </>
  );

  if (unknownPlan) {
    // its figures would all be zero: never an all-clear page for a plan that does not exist (SDD §16)
    return (
      <>
        {header}
        <EmptyState
          icon={SearchX}
          title={`Plan ${planId} does not exist`}
          description="It may have been mistyped or come from an old link."
          action={
            <Button variant="secondary" onClick={() => setPlan('')}>
              Show all plans
            </Button>
          }
        />
      </>
    );
  }

  return (
    <>
      {header}
      {stats.error && <ErrorBanner error={stats.error} title="Statistics are unavailable" onRetry={() => void stats.refetch()} className="mb-4" />}

      {migrationsQuery.error && <ErrorBanner error={migrationsQuery.error} title="Migrations are unavailable" onRetry={() => void migrationsQuery.refetch()} className="mt-4" />}

      <div className="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold">Operations</h2>
        <a href="#performance" className={buttonClassName('ghost', 'md')}>
          <ChartNoAxesCombined aria-hidden className="size-4" /> View metrics
        </a>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="grid min-w-0 gap-4">
          {migrationsQuery.isPending ? (
            <LoadingBlock label="Loading the downtime clocks…" />
          ) : migrationsUnknown ? (
            <Panel id="downtime" title="Downtime now" description="VMs whose source is stopped appear here with a running clock.">
              <Unavailable what="migrations" />
            </Panel>
          ) : (
            <DowntimeNow active={down} waiting={waiting} plans={plansById} plansUnknown={plansUnknown} />
          )}
          {queuesLoading ? <LoadingBlock label="Loading pending approvals…" /> : (
            <PendingApprovals key={planId} queue={migrationsUnknown ? null : approvals} />
          )}
        </div>
        {queuesLoading ? (
          <LoadingBlock label="Loading items that need attention…" />
        ) : migrationsUnknown ? (
          <Panel id="attention" title="Needs attention" description="The next human action, most urgent first">
            <Unavailable what="migrations" />
          </Panel>
        ) : (
          <NeedsAttention key={planId} items={attention} plansUnknown={plansUnknown} />
        )}
      </div>

      <h2 id="performance" tabIndex={-1} className="mt-6 mb-3 scroll-mt-20 rounded-sm text-lg font-semibold">Performance</h2>
      {!s ? (
        statsUnknown ? (
          <MetricStrip
            label="Key metrics"
            metrics={['Migrations', 'In progress', 'Awaiting finalize', 'Finalized', 'Failed', 'Rolled back', 'Average downtime', 'SLO compliance'].map((label) => ({ label, value: DASH }))}
          />
        ) : (
          <Skeleton className="h-[86px]" />
        )
      ) : (
        <MetricStrip
          label="Key metrics"
          busy={stats.isPlaceholderData}
          metrics={[
            {
              label: 'Migrations',
              value: formatNumber(counted),
              hint: [plansUnknown ? 'plans unknown' : `in ${scopedPlans.length} plan${scopedPlans.length === 1 ? '' : 's'}`, cancelled ? `${formatNumber(cancelled)} cancelled, not counted` : '']
                .filter(Boolean)
                .join('; '),
            },
            { label: 'In progress', value: formatNumber(s.in_progress), hint: 'started, unfinished' },
            // Completed, split: verified with the source VM kept, and finalized (SDD §4.2, §16, ADV-19)
            { label: 'Awaiting finalize', ...phaseMetric('completed', s.awaiting_finalize), hint: 'verified; source VM kept' },
            { label: 'Finalized', ...phaseMetric('finalized', s.finalized), hint: counted ? `${formatPct((s.finalized / counted) * 100)} of all` : undefined },
            { label: 'Failed', ...phaseMetric('failed', s.failed), tone: s.failed > 0 ? 'danger' : undefined, hint: s.failed > 0 ? 'retry, roll back or cancel' : 'none' },
            // its source VM runs again and a person retries or cancels it: never all clear while one waits (SDD §16)
            {
              label: 'Rolled back',
              ...phaseMetric('rolled_back', s.by_phase.rolled_back ?? 0),
              tone: (s.by_phase.rolled_back ?? 0) > 0 ? 'warning' : undefined,
              hint: (s.by_phase.rolled_back ?? 0) > 0 ? 'source running: retry or cancel' : 'none',
            },
            {
              label: 'Average downtime',
              value: formatDuration(s.avg_downtime_s),
              hint: [`p95 ${formatDuration(s.p95_downtime_s)}, max ${formatDuration(s.max_downtime_s)}`, leftOutText(s)].filter(Boolean).join('; '),
            },
            { label: 'SLO compliance', value: formatPct(s.slo_compliance_pct), hint: ['verified within plan SLO', leftOutText(s)].filter(Boolean).join('; ') },
          ]}
        />
      )}

      <div className="mt-4 grid gap-4 xl:grid-cols-2">
        <Panel title="Downtime vs SLO" description="Average verified downtime per strategy">
          {s ? (
            <>
              {plansUnknown && <Unavailable what="plans" className="mb-2" />}
              <DowntimeSloChart byStrategy={s.downtime_by_strategy} sloS={sloS} sloLabel={sloLabel} />
            </>
          ) : statsUnknown ? (
            <Unavailable what="statistics" />
          ) : (
            <Skeleton className="h-40" />
          )}
        </Panel>
        <Panel title="Phase distribution" description={s ? `${formatNumber(s.total)} migrations by phase` : undefined}>
          {s ? <PhaseDistribution byPhase={s.by_phase} /> : statsUnknown ? <Unavailable what="statistics" /> : <LoadingBlock rows={6} />}
        </Panel>
      </div>

      <Panel
        title="Throughput"
        description={s ? `Last 60 minutes, ${formatBytes(s.bytes_transferred)} transferred in total` : 'Last 60 minutes'}
        className="mt-4"
      >
        {s ? <ThroughputChart series={s.throughput_series} /> : statsUnknown ? <Unavailable what="statistics" /> : <Skeleton className="h-56" />}
      </Panel>

      <Panel title="Plans" description="Completion per plan" className="mt-4" actions={
        <Link to="/plans" className={buttonClassName('secondary', 'lg')}>
          View all plans <ArrowRight aria-hidden className="size-4 shrink-0" />
        </Link>
      }>
        {plansQuery.isPending || migrationsQuery.isPending ? (
          <LoadingBlock rows={4} />
        ) : plansUnknown ? (
          <Unavailable what="plans" />
        ) : (
          <PlanProgress plans={scopedPlans} migrations={migrationsUnknown ? null : migrations} />
        )}
      </Panel>
    </>
  );
}
