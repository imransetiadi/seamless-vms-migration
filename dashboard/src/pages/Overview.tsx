import {
  Activity,
  CircleCheck,
  CircleX,
  ClipboardList,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMigrations, usePlans, useStats } from '../api/hooks';
import type { Migration, Phase, Plan } from '../api/types';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { DowntimeSloChart } from '../components/DowntimeSloChart';
import { SelectField } from '../components/Field';
import { MetricStrip } from '../components/MetricStrip';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { PhaseDistribution } from '../components/PhaseDistribution';
import { ProgressBar } from '../components/ProgressBar';
import { LoadingBlock, Skeleton } from '../components/Skeleton';
import { PhaseBadge, PlanStatusBadge } from '../components/StatusBadge';
import { ThroughputChart } from '../components/ThroughputChart';
import { attentionItems } from '../lib/attention';
import { cn } from '../lib/cn';
import { formatBytes, formatClock, formatDuration, formatNumber, formatPct, secondsBetween } from '../lib/format';
import { strategyLabel, TONE_CLASSES } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';
import { useNow } from '../lib/useNow';

const ATTENTION_ICONS: Record<'danger' | 'warning' | 'info', LucideIcon> = {
  danger: CircleX,
  warning: TriangleAlert,
  info: Activity,
};

/** Share of the downtime SLO used: below 75 % calm, then a warning, above 100 % a breach. */
function budgetTone(used: number): 'success' | 'warning' | 'danger' {
  if (used > 1) return 'danger';
  if (used >= 0.75) return 'warning';
  return 'success';
}

const BAR_CLASSES: Record<'success' | 'warning' | 'danger', string> = {
  success: 'bg-status-success',
  warning: 'bg-status-warning',
  danger: 'bg-status-danger',
};

/**
 * Down right now: in the cutover window (the stop may not be reported yet) or with an open downtime
 * clock — a failed cutover or a rollback keeps the source stopped until it runs again (SDD §5.2).
 */
function isDown(m: Migration): boolean {
  return m.phase === 'cutover' || m.phase === 'verifying' || (Boolean(m.downtime_started_at) && !m.downtime_ended_at);
}

/** What the VM is waiting for while it is down, after the strategy and plan. */
const DOWN_DETAIL: Partial<Record<Phase, string>> = {
  verifying: ', checking the boot on RHOSO',
  rolling_back: ', rolling back, restarting the source VM',
  failed: ', failed with the source stopped: roll back or retry',
};

/**
 * The page's centre: every VM whose source is stopped right now, with its downtime clock and how
 * much of the plan's downtime SLO it has used (SDD §7.2 downtime clock, §5.4).
 */
function DowntimeNow({ migrations, plans }: { migrations: Migration[]; plans: ReadonlyMap<string, Plan> }) {
  const active = migrations
    .filter(isDown)
    .sort((a, b) => Date.parse(a.downtime_started_at ?? a.updated_at) - Date.parse(b.downtime_started_at ?? b.updated_at));
  const waiting = migrations.filter((m) => m.phase === 'awaiting_cutover').length;
  const now = useNow(1000, active.length > 0);

  return (
    <Panel
      title="Downtime now"
      description={
        active.length
          ? `${active.length} VM${active.length === 1 ? ' is' : 's are'} down; each clock stops when the VM boots verified on RHOSO or its source runs again.`
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
            const elapsed = secondsBetween(m.downtime_started_at, now) ?? 0;
            const used = slo ? elapsed / slo : 0;
            const tone = budgetTone(used);
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
                {slo !== null && (
                  <div className="flex flex-col gap-1">
                    <div
                      role="meter"
                      aria-label={`${m.vm.name} downtime against the SLO`}
                      aria-valuemin={0}
                      aria-valuemax={slo}
                      aria-valuenow={Math.min(elapsed, slo)}
                      aria-valuetext={`${formatDuration(elapsed)} of ${formatDuration(slo)}`}
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
                      {tone === 'danger' && <TriangleAlert aria-hidden className="size-3.5" />}
                      {left !== null && left >= 0
                        ? `${formatDuration(left)} left in the ${formatDuration(slo)} SLO`
                        : `Over the SLO by ${formatDuration(-(left ?? 0))}`}
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

function NeedsAttention({ migrations, plans }: { migrations: Migration[]; plans: ReadonlyMap<string, Plan> }) {
  const items = attentionItems(migrations, plans);
  const shown = items.slice(0, 8);
  return (
    <Panel title="Needs attention" description="The next human action, most urgent first">
      {items.length === 0 ? (
        <EmptyState icon={CircleCheck} title="Nothing needs attention" description="No failures, approvals or blockers are waiting." />
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

function PlanProgress({ plans, migrations }: { plans: Plan[]; migrations: Migration[] }) {
  if (plans.length === 0) {
    return <EmptyState icon={ClipboardList} title="No plans yet" description="Create a plan to start migrating." />;
  }
  return (
    // -mt-px hides the top rule of the first row in every column
    <div className="-my-2 overflow-hidden">
    <ul className="-mt-px grid gap-x-8 md:grid-cols-2 2xl:grid-cols-3">
      {plans.map((plan) => {
        const mine = migrations.filter((m) => m.plan_id === plan.id);
        const done = mine.filter((m) => ['completed', 'finalized'].includes(m.phase)).length;
        const pct = mine.length ? (done / mine.length) * 100 : 0;
        return (
          <li key={plan.id} className="flex min-w-0 flex-col gap-1.5 border-t border-border py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Link to={`/plans/${plan.id}`} className="min-w-0 truncate font-medium text-foreground underline-offset-4 hover:underline">
                {plan.name}
              </Link>
              <PlanStatusBadge status={plan.status} />
            </div>
            <ProgressBar value={pct} label={`${plan.name}: migrations done`} tone="success" size="sm" />
            <p className="num text-xs text-muted-foreground">
              {done} of {mine.length} done, downtime SLO {formatDuration(plan.downtime_slo_s)}
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
  const migrationsQuery = useMigrations(planId ? { plan_id: planId } : {}, { keepPrevious: true });

  const plans = useMemo(() => plansQuery.data ?? [], [plansQuery.data]);
  const plansById = useMemo(() => new Map(plans.map((p) => [p.id, p])), [plans]);
  const migrations = migrationsQuery.data ?? [];
  const scopedPlans = planId ? plans.filter((p) => p.id === planId) : plans;
  const activePlans = scopedPlans.filter((p) => p.status !== 'draft');
  const sloS = activePlans.length ? Math.min(...activePlans.map((p) => p.downtime_slo_s)) : null;
  const sloLabel = planId || activePlans.length <= 1 ? 'SLO' : 'Strictest SLO';
  const s = stats.data;

  const setPlan = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set('plan', value);
    else next.delete('plan');
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageHeader
        title="Overview"
        description="Live state of VM migrations into Red Hat OpenStack Services on OpenShift 18.0."
        actions={
          <SelectField
            label="Plan"
            className="w-full sm:w-72"
            value={planId}
            onChange={(e) => setPlan(e.target.value)}
            options={[{ value: '', label: 'All plans' }, ...plans.map((p) => ({ value: p.id, label: p.name }))]}
          />
        }
      />

      {stats.error && <ErrorBanner error={stats.error} title="Statistics are unavailable" onRetry={() => void stats.refetch()} className="mb-4" />}

      {!s ? (
        <Skeleton className="h-[86px]" />
      ) : (
        <MetricStrip
          label="Key metrics"
          className={cn(stats.isPlaceholderData && 'opacity-60')}
          metrics={[
            { label: 'Migrations', value: formatNumber(s.total), hint: `in ${scopedPlans.length} plan${scopedPlans.length === 1 ? '' : 's'}` },
            { label: 'In progress', value: formatNumber(s.in_progress), hint: 'started, not yet finished' },
            { label: 'Completed', value: formatNumber(s.completed), hint: s.total ? `${formatPct((s.completed / s.total) * 100)} of all` : undefined },
            { label: 'Failed', value: formatNumber(s.failed), tone: s.failed > 0 ? 'danger' : undefined, hint: s.failed > 0 ? 'retry, roll back or cancel' : 'none' },
            { label: 'Average downtime', value: formatDuration(s.avg_downtime_s), hint: `p95 ${formatDuration(s.p95_downtime_s)}, max ${formatDuration(s.max_downtime_s)}` },
            { label: 'SLO compliance', value: formatPct(s.slo_compliance_pct), hint: 'verified cutovers within their plan SLO' },
          ]}
        />
      )}

      {migrationsQuery.error && <ErrorBanner error={migrationsQuery.error} title="Migrations are unavailable" onRetry={() => void migrationsQuery.refetch()} className="mt-4" />}

      {/* DOM order is the phone order: live clocks, then the human actions, then the history.
          On wide screens the actions take the right column beside both downtime panels. */}
      <div className="mt-4 grid items-start gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] xl:grid-rows-[auto_1fr]">
        {migrationsQuery.isPending ? (
          <LoadingBlock label="Loading the downtime clocks…" />
        ) : (
          <DowntimeNow migrations={migrations} plans={plansById} />
        )}
        <div className="min-w-0 xl:col-start-2 xl:row-span-2 xl:row-start-1">
          {migrationsQuery.isPending ? (
            <LoadingBlock label="Loading items that need attention…" />
          ) : (
            <NeedsAttention migrations={migrations} plans={plansById} />
          )}
        </div>
        <Panel title="Downtime vs SLO" description="Average verified downtime per strategy">
          {s ? <DowntimeSloChart byStrategy={s.downtime_by_strategy} sloS={sloS} sloLabel={sloLabel} /> : <Skeleton className="h-40" />}
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Panel
          title="Throughput"
          description={s ? `Last 60 minutes, ${formatBytes(s.bytes_transferred)} transferred in total` : 'Last 60 minutes'}
          className="xl:col-span-2"
        >
          {s ? <ThroughputChart series={s.throughput_series} /> : <Skeleton className="h-56" />}
        </Panel>
        <Panel title="Phase distribution" description={s ? `${formatNumber(s.total)} migrations by phase` : undefined}>
          {s ? <PhaseDistribution byPhase={s.by_phase} /> : <LoadingBlock rows={6} />}
        </Panel>
      </div>

      <Panel title="Plans" description="Completion per plan" className="mt-4">
        {plansQuery.isPending ? <LoadingBlock rows={4} /> : <PlanProgress plans={scopedPlans} migrations={migrations} />}
      </Panel>
    </>
  );
}
