import {
  Activity,
  CircleCheck,
  CircleHelp,
  CircleX,
  ClipboardList,
  SearchX,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useDemoSpeed, useMigrationSummaries, usePlans, useStats } from '../api/hooks';
import type { MigrationSummary, Phase, Plan } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { DowntimeSloChart } from '../components/DowntimeSloChart';
import { SelectField } from '../components/Field';
import { MetricStrip } from '../components/MetricStrip';
import { PendingApprovals } from '../components/PendingApprovals';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { PhaseDistribution } from '../components/PhaseDistribution';
import { ProgressBar } from '../components/ProgressBar';
import { LoadingBlock, Skeleton } from '../components/Skeleton';
import { PhaseBadge, PlanStatusBadge } from '../components/StatusBadge';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { ThroughputChart } from '../components/ThroughputChart';
import { Unavailable } from '../components/Unavailable';
import { attentionItems } from '../lib/attention';
import { cn } from '../lib/cn';
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
function DowntimeNow({ migrations, plans, plansUnknown }: { migrations: MigrationSummary[]; plans: ReadonlyMap<string, Plan>; plansUnknown: boolean }) {
  const active = migrations
    .filter(isDown)
    .sort((a, b) => Date.parse(a.downtime_started_at ?? a.updated_at) - Date.parse(b.downtime_started_at ?? b.updated_at));
  const waiting = migrations.filter((m) => m.phase === 'awaiting_cutover').length;
  // in demo mode the clocks run in model time, as on the migration page (SDD §16, ADV-16)
  const speed = speedOf(useDemoSpeed());
  const now = useNow(speed > 1 ? 250 : 1000, active.length > 0);
  const demoNote = demoTimeText(speed);

  return (
    <Panel
      title="Downtime now"
      description={
        active.length
          ? `${active.length} VM${active.length === 1 ? ' is' : 's are'} down; each clock stops when the VM boots verified on the destination or its source runs again.${demoNote ? ` ${demoNote}.` : ''}`
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
                      <Link to={`/migrations/${m.id}`} className="font-mono text-base font-semibold text-foreground underline-offset-4 hover:underline">
                        {m.vm.name}
                      </Link>
                      <PhaseBadge phase={m.phase} />
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {strategyLabel(m.strategy)}
                      {plan ? ` in ${plan.name}` : ''}
                      {m.phase === 'cutover' ? `, final copy ${formatPct(m.progress_pct)}` : (DOWN_DETAIL[m.phase] ?? ', source VM stopped')}
                    </p>
                  </div>
                  <p className="flex items-baseline gap-2">
                    <span className="sr-only">Downtime </span>
                    <span className={cn('num text-4xl font-semibold leading-none tracking-tight', tone === 'success' ? 'text-foreground' : TONE_CLASSES[tone].text)}>
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

function NeedsAttention({ migrations, plans, plansUnknown }: { migrations: MigrationSummary[]; plans: ReadonlyMap<string, Plan>; plansUnknown: boolean }) {
  // the time, read once per render (useNow): a cutover window that opens or closes shows within a minute
  const now = useNow(60_000);
  const items = attentionItems(migrations, plans, now, { plansUnknown });
  const shown = items.slice(0, 8);
  return (
    <Panel title="Needs attention" description="The next human action, most urgent first">
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
                <li key={item.migration.id} className="flex gap-3 py-2.5">
                  <Icon aria-hidden className={cn('mt-0.5 size-4 shrink-0', TONE_CLASSES[item.tone].text)} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        to={`/migrations/${item.migration.id}`}
                        className="font-mono text-sm font-semibold text-foreground underline-offset-4 hover:underline"
                      >
                        {item.migration.vm.name}
                      </Link>
                      <PhaseBadge phase={item.migration.phase} />
                      {item.techPreview && <TechPreviewBadge />}
                    </div>
                    <p className="mt-0.5 wrap-break-word text-sm text-foreground">{item.reason}</p>
                    <p className="text-xs text-muted-foreground">Next: {item.nextAction}</p>
                  </div>
                </li>
              );
            })}
          </ul>
          {items.length > shown.length && (
            <p className="mt-3 text-xs text-muted-foreground">
              {items.length - shown.length} more in the <Link to="/plans" className="underline underline-offset-4">plans</Link>.
            </p>
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
              {/* one unit: a narrow card breaks the line before it, never inside the duration */}
              <span className="whitespace-nowrap">downtime SLO {formatDuration(plan.downtime_slo_s)}</span>
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
  const plansQuery = usePlans();
  const stats = useStats(planId || null);
  // summaries: what the page's rules read, not every migration whole (SDD §16, perf audit PF-02)
  const migrationsQuery = useMigrationSummaries(planId ? { plan_id: planId } : {}, { keepPrevious: true });

  const plans = useMemo(() => plansQuery.data ?? [], [plansQuery.data]);
  const plansById = useMemo(() => new Map(plans.map((p) => [p.id, p])), [plans]);
  const migrations = migrationsQuery.data ?? [];
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

  const setPlan = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set('plan', value);
    else next.delete('plan');
    setParams(next, { replace: true });
  };

  const header = (
    <>
      <PageHeader
        title="Overview"
        description={`Live state of VM migrations into ${DESTINATIONS}.`}
        actions={
          <SelectField
            label="Plan"
            className="w-full sm:w-72"
            value={planId}
            onChange={(e) => setPlan(e.target.value)}
            options={[
              { value: '', label: 'All plans' },
              ...plans.map((p) => ({ value: p.id, label: p.name })),
              // a plan in the address that the list does not hold stays chosen (SDD §16), as on Events
              ...(planId && !plansById.has(planId) ? [{ value: planId, label: planId }] : []),
            ]}
          />
        }
      />

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
            { label: 'In progress', value: formatNumber(s.in_progress), hint: 'started, not yet finished' },
            // Completed, split: verified with the source VM kept, and finalized (SDD §4.2, §16, ADV-19)
            { label: 'Awaiting finalize', value: formatNumber(s.awaiting_finalize), hint: 'verified; the source VM is kept' },
            { label: 'Finalized', value: formatNumber(s.finalized), hint: counted ? `${formatPct((s.finalized / counted) * 100)} of all` : undefined },
            { label: 'Failed', value: formatNumber(s.failed), tone: s.failed > 0 ? 'danger' : undefined, hint: s.failed > 0 ? 'retry, roll back or cancel' : 'none' },
            // its source VM runs again and a person retries or cancels it: never all clear while one waits (SDD §16)
            {
              label: 'Rolled back',
              value: formatNumber(s.by_phase.rolled_back ?? 0),
              tone: (s.by_phase.rolled_back ?? 0) > 0 ? 'warning' : undefined,
              hint: (s.by_phase.rolled_back ?? 0) > 0 ? 'the source VM runs again: retry or cancel' : 'none',
            },
            {
              label: 'Average downtime',
              value: formatDuration(s.avg_downtime_s),
              hint: [`p95 ${formatDuration(s.p95_downtime_s)}, max ${formatDuration(s.max_downtime_s)}`, leftOutText(s)].filter(Boolean).join('; '),
            },
            { label: 'SLO compliance', value: formatPct(s.slo_compliance_pct), hint: ['verified cutovers within their plan SLO', leftOutText(s)].filter(Boolean).join('; ') },
          ]}
        />
      )}

      {migrationsQuery.error && <ErrorBanner error={migrationsQuery.error} title="Migrations are unavailable" onRetry={() => void migrationsQuery.refetch()} className="mt-4" />}

      {/* DOM order is the phone order: live clocks, then the human actions, then the history.
          On wide screens the actions take the right column beside both downtime panels. */}
      <div className="mt-4 grid items-start gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] xl:grid-rows-[auto_1fr]">
        {migrationsQuery.isPending ? (
          <LoadingBlock label="Loading the downtime clocks…" />
        ) : migrationsUnknown ? (
          <Panel title="Downtime now" description="VMs whose source is stopped appear here with a running clock.">
            <Unavailable what="migrations" />
          </Panel>
        ) : (
          <DowntimeNow migrations={migrations} plans={plansById} plansUnknown={plansUnknown} />
        )}
        <div className="flex min-w-0 flex-col gap-4 xl:col-start-2 xl:row-span-2 xl:row-start-1">
          {migrationsQuery.isPending || plansQuery.isPending ? (
            <LoadingBlock label="Loading items that need attention…" />
          ) : (
            <>
              {/* a pending approval is listed once, here, not in Needs attention (SDD §16, ADV-20) */}
              <PendingApprovals migrations={migrationsUnknown ? null : migrations} plans={plansById} plansUnknown={plansUnknown} />
              {migrationsUnknown ? (
                <Panel title="Needs attention" description="The next human action, most urgent first">
                  <Unavailable what="migrations" />
                </Panel>
              ) : (
                <NeedsAttention migrations={migrations} plans={plansById} plansUnknown={plansUnknown} />
              )}
            </>
          )}
        </div>
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
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Panel
          title="Throughput"
          description={s ? `Last 60 minutes, ${formatBytes(s.bytes_transferred)} transferred in total` : 'Last 60 minutes'}
          className="xl:col-span-2"
        >
          {s ? <ThroughputChart series={s.throughput_series} /> : statsUnknown ? <Unavailable what="statistics" /> : <Skeleton className="h-56" />}
        </Panel>
        <Panel title="Phase distribution" description={s ? `${formatNumber(s.total)} migrations by phase` : undefined}>
          {s ? <PhaseDistribution byPhase={s.by_phase} /> : statsUnknown ? <Unavailable what="statistics" /> : <LoadingBlock rows={6} />}
        </Panel>
      </div>

      <Panel title="Plans" description="Completion per plan" className="mt-4">
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
