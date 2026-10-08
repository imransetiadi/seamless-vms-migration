import {
  Activity,
  CircleCheck,
  CircleX,
  ClipboardList,
  Gauge,
  Layers,
  Timer,
  TriangleAlert,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMigrations, usePlans, useStats } from '../api/hooks';
import type { Migration, Plan } from '../api/types';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { DowntimeSloChart } from '../components/DowntimeSloChart';
import { SelectField } from '../components/Field';
import { KpiTile } from '../components/KpiTile';
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

function ActiveCutovers({ migrations, plans }: { migrations: Migration[]; plans: ReadonlyMap<string, Plan> }) {
  const active = migrations
    .filter((m) => m.phase === 'cutover' || m.phase === 'verifying')
    .sort((a, b) => Date.parse(a.downtime_started_at ?? a.updated_at) - Date.parse(b.downtime_started_at ?? b.updated_at));
  const now = useNow(1000, active.length > 0);

  return (
    <Panel title="Active cutovers" description="Source VM stopped — the downtime clock is running">
      {active.length === 0 ? (
        <EmptyState icon={Zap} title="No cutover in progress" description="Migrations enter cutover once approved, inside their window." />
      ) : (
        <ul className="-my-2 divide-y divide-border">
          {active.map((m) => {
            const plan = plans.get(m.plan_id);
            const slo = plan?.downtime_slo_s ?? null;
            const elapsed = secondsBetween(m.downtime_started_at, now);
            const over = slo !== null && elapsed !== null && elapsed > slo;
            return (
              <li key={m.id} className="grid gap-3 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                <div className="flex min-w-0 flex-col gap-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link to={`/migrations/${m.id}`} className="font-mono font-semibold text-foreground underline-offset-4 hover:underline">
                      {m.vm.name}
                    </Link>
                    <PhaseBadge phase={m.phase} />
                    <span className="text-xs text-muted-foreground">
                      {strategyLabel(m.strategy)}
                      {plan ? ` · ${plan.name}` : ''}
                    </span>
                  </div>
                  <ProgressBar value={m.progress_pct} label={`${m.vm.name} ${m.phase} progress`} size="sm" />
                </div>
                <div className="flex items-baseline gap-3 sm:flex-col sm:items-end sm:gap-0">
                  <span className="text-xs text-muted-foreground">Downtime</span>
                  <span className={cn('num text-xl font-semibold', over ? 'text-status-danger' : 'text-foreground')}>{formatClock(elapsed)}</span>
                  {slo !== null && (
                    <span className={cn('inline-flex items-center gap-1 text-xs', over ? 'text-status-danger' : 'text-muted-foreground')}>
                      {over && <TriangleAlert aria-hidden className="size-3.5" />}
                      {over ? 'Over' : 'SLO'} {formatDuration(slo)}
                    </span>
                  )}
                </div>
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
              {items.length - shown.length} more — see the <Link to="/plans" className="underline underline-offset-4">plans</Link>.
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
    <ul className="-my-2 divide-y divide-border">
      {plans.map((plan) => {
        const mine = migrations.filter((m) => m.plan_id === plan.id);
        const done = mine.filter((m) => ['completed', 'finalized'].includes(m.phase)).length;
        const pct = mine.length ? (done / mine.length) * 100 : 0;
        return (
          <li key={plan.id} className="flex flex-col gap-1.5 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Link to={`/plans/${plan.id}`} className="min-w-0 truncate font-medium text-foreground underline-offset-4 hover:underline">
                {plan.name}
              </Link>
              <PlanStatusBadge status={plan.status} />
            </div>
            <ProgressBar value={pct} label={`${plan.name}: migrations done`} tone="success" size="sm" />
            <p className="num text-xs text-muted-foreground">
              {done}/{mine.length} done · SLO {formatDuration(plan.downtime_slo_s)}
            </p>
          </li>
        );
      })}
    </ul>
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

      <section aria-label="Key metrics" className={cn('grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6', stats.isPlaceholderData && 'opacity-60')}>
        {!s ? (
          Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-[92px]" />)
        ) : (
          <>
            <KpiTile label="Migrations" value={formatNumber(s.total)} icon={Layers} hint={`${scopedPlans.length} plan${scopedPlans.length === 1 ? '' : 's'}`} />
            <KpiTile label="In progress" value={formatNumber(s.in_progress)} icon={Activity} tone="progress" hint="validating, copying, cutting over" />
            <KpiTile label="Completed" value={formatNumber(s.completed)} icon={CircleCheck} tone="success" hint={s.total ? `${formatPct((s.completed / s.total) * 100)} of all` : undefined} />
            <KpiTile label="Failed" value={formatNumber(s.failed)} icon={CircleX} tone={s.failed > 0 ? 'danger' : 'neutral'} hint={s.failed > 0 ? 'needs retry, rollback or cancel' : 'none'} />
            <KpiTile
              label="Avg downtime"
              value={formatDuration(s.avg_downtime_s)}
              icon={Timer}
              hint={`p95 ${formatDuration(s.p95_downtime_s)} · max ${formatDuration(s.max_downtime_s)}`}
            />
            <KpiTile label="SLO compliance" value={formatPct(s.slo_compliance_pct)} icon={Gauge} hint="verified cutovers within their plan SLO" />
          </>
        )}
      </section>

      {migrationsQuery.error && <ErrorBanner error={migrationsQuery.error} title="Migrations are unavailable" onRetry={() => void migrationsQuery.refetch()} className="mt-4" />}

      <div className="mt-4 grid items-start gap-4 xl:grid-cols-2">
        {migrationsQuery.isPending ? (
          <>
            <LoadingBlock label="Loading active cutovers…" />
            <LoadingBlock label="Loading items that need attention…" />
          </>
        ) : (
          <>
            <ActiveCutovers migrations={migrations} plans={plansById} />
            <NeedsAttention migrations={migrations} plans={plansById} />
          </>
        )}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <Panel
          title="Throughput"
          description={s ? `Last 60 minutes · ${formatBytes(s.bytes_transferred)} transferred in total` : 'Last 60 minutes'}
          className="xl:col-span-2"
        >
          {s ? <ThroughputChart series={s.throughput_series} /> : <Skeleton className="h-56" />}
        </Panel>
        <Panel title="Phase distribution" description={s ? `${formatNumber(s.total)} migrations by phase` : undefined}>
          {s ? <PhaseDistribution byPhase={s.by_phase} /> : <LoadingBlock rows={6} />}
        </Panel>
      </div>

      <div className="mt-4 grid items-start gap-4 xl:grid-cols-2">
        <Panel title="Downtime vs SLO" description="Average verified downtime per strategy">
          {s ? <DowntimeSloChart byStrategy={s.downtime_by_strategy} sloS={sloS} sloLabel={sloLabel} /> : <Skeleton className="h-40" />}
        </Panel>
        <Panel title="Plans" description="Completion per plan">
          {plansQuery.isPending ? <LoadingBlock rows={4} /> : <PlanProgress plans={scopedPlans} migrations={migrations} />}
        </Panel>
      </div>
    </>
  );
}
