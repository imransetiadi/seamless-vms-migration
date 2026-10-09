import {
  Activity,
  CircleCheck,
  CircleX,
  ClipboardX,
  Gauge,
  Layers,
  ListChecks,
  Pause,
  Pencil,
  Play,
  Rows3,
  Timer,
  TriangleAlert,
} from 'lucide-react';
import { useId, useMemo, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { useMigrations, usePlan, usePlanAction, useProviders, useStats, type PlanActionRequest } from '../api/hooks';
import { useRole } from '../api/session';
import type { Plan, Provider, ValidationReport } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PlanCreateDialog } from '../components/PlanCreateDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { TextField } from '../components/Field';
import { FindingGroupsList, type FindingRow } from '../components/FindingsList';
import { KpiTile } from '../components/KpiTile';
import { MigrationsTable } from '../components/MigrationsTable';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { PlanStatusBadge } from '../components/StatusBadge';
import { WavesBoard } from '../components/WavesBoard';
import { cn } from '../lib/cn';
import { groupFindings } from '../lib/findings';
import { formatBytes, formatDateTime, formatDuration, formatNumber, formatPct, formatRate, formatRelative } from '../lib/format';
import { planActions } from '../lib/planActions';
import { PROVIDER_KIND_LABELS, strategyLabel } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

type Confirm = 'start' | 'pause' | 'waves' | null;

function Setting({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 wrap-break-word text-sm text-foreground">{children}</dd>
    </div>
  );
}

function MappingList({ title, map }: { title: string; map: Record<string, string> }) {
  const entries = Object.entries(map);
  if (entries.length === 0) return null;
  return (
    <div>
      <p className="text-xs text-muted-foreground">{title}</p>
      <ul className="font-mono text-xs">
        {entries.map(([from, to]) => (
          <li key={from} className="break-all">
            {from} → {to}
          </li>
        ))}
      </ul>
    </div>
  );
}

function providerLabel(provider: Provider | undefined, id: string) {
  if (!provider) return <code>{id}</code>;
  return (
    <Link to={`/inventory/${provider.id}`} className="underline underline-offset-4">
      {provider.name}
    </Link>
  );
}

/** Human value of an estimator override (SDD §9.1): rates, durations, counts; link_bps is ignored. */
function formatOverride(key: string, value: number): string {
  if (key === 'link_bps') return `${formatRate(value)} (ignored: the plan link bandwidth applies)`;
  if (key.endsWith('_bps')) return formatRate(value);
  if (key.endsWith('_s')) return formatDuration(value);
  return String(value);
}

function windowText(plan: Plan): string {
  const w = plan.cutover_window;
  if (!w) return 'Any time';
  const now = Date.now();
  const open = now >= Date.parse(w.start) && now <= Date.parse(w.end);
  const when = open ? 'open now' : Date.parse(w.start) > now ? `opens ${formatRelative(w.start)}` : 'closed';
  return `${formatDateTime(w.start)} → ${formatDateTime(w.end)} (${when})`;
}

function SettingsSummary({ plan, providers }: { plan: Plan; providers: Provider[] }) {
  const source = providers.find((p) => p.id === plan.source_provider_id);
  const destination = providers.find((p) => p.id === plan.destination_provider_id);
  const overrides = Object.keys(plan.strategy_overrides).length;
  const v = plan.verification;
  const mappingCount = Object.values(plan.mappings).reduce((n, m) => n + Object.keys(m).length, 0);
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-4">
      <Setting label="Source">
        {providerLabel(source, plan.source_provider_id)}
        {source && <span className="block text-xs text-muted-foreground">{PROVIDER_KIND_LABELS[source.kind]}</span>}
      </Setting>
      <Setting label="Destination">{providerLabel(destination, plan.destination_provider_id)}</Setting>
      <Setting label="Downtime SLO">
        <span className="num">{formatDuration(plan.downtime_slo_s)}</span> per VM
      </Setting>
      <Setting label="Strategy">
        {strategyLabel(plan.default_strategy)} · {plan.selection_policy === 'min_downtime' ? 'minimum downtime' : 'simplest meeting the SLO'}
        {overrides > 0 && <span className="block text-xs text-muted-foreground">{overrides} per-VM override(s)</span>}
      </Setting>
      <Setting label="Require approval">{plan.require_approval ? 'Yes — an approver must approve each cutover' : 'No'}</Setting>
      <Setting label="Cutover trigger">{plan.auto_cutover ? 'Automatic once the gate opens' : 'On request (Cutover action)'}</Setting>
      <Setting label="Cutover window">{windowText(plan)}</Setting>
      <Setting label="Warm sync">
        converge below <span className="num">{formatBytes(plan.convergence_threshold_bytes)}</span> · max {plan.max_sync_passes} passes · keep-warm
        every {formatDuration(plan.keep_warm_interval_s)}
      </Setting>
      <Setting label="Link bandwidth">
        <span className="num">{formatRate(plan.link_bps)}</span>
      </Setting>
      {Object.keys(plan.estimator_overrides).length > 0 && (
        <Setting label="Estimator overrides">
          <span className="font-mono text-xs">
            {Object.entries(plan.estimator_overrides)
              .map(([k, v]) => `${k}=${formatOverride(k, v)}`)
              .join(' · ')}
          </span>
          <span className="block text-xs text-muted-foreground">Replace the planning defaults for this plan (SDD §9.1)</span>
        </Setting>
      )}
      <Setting label="Storage handover">
        {plan.handover.enabled
          ? Object.entries(plan.handover.backend_map)
              .map(([type, target]) => `${type} to ${target}`)
              .join(', ') || 'Enabled, no backend mapped'
          : 'Disabled'}
      </Setting>
      <Setting label="Verification">
        {v.tcp_ports.length ? `TCP ${v.tcp_ports.join(', ')}` : 'No TCP probes'} ·{' '}
        {v.windows_tcp_ports?.length ? `Windows TCP ${v.windows_tcp_ports.join(', ')}` : 'no Windows TCP probes'} · {v.probe_address} IP · timeout {formatDuration(v.timeout_s)} ·
        auto-rollback {v.auto_rollback ? 'on' : 'off'} · advisor {v.use_advisor ? 'on' : 'off'}
      </Setting>
      <Setting label="Pre-staged resources">{plan.prestage_resources.join(', ') || '—'}</Setting>
      {mappingCount > 0 && (
        <div className="min-w-0 sm:col-span-2 xl:col-span-4">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">Mappings</dt>
          <dd className="mt-0.5">
            <details>
              <summary className="inline-flex min-h-8 cursor-pointer items-center text-sm text-muted-foreground hover:text-foreground">
                {mappingCount} source → destination mapping{mappingCount === 1 ? '' : 's'}
              </summary>
              <div className="mt-2 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <MappingList title="Networks" map={plan.mappings.networks} />
                <MappingList title="Flavors" map={plan.mappings.flavors} />
                <MappingList title="Volume types" map={plan.mappings.volume_types} />
                <MappingList title="Projects" map={plan.mappings.projects} />
              </div>
            </details>
          </dd>
        </div>
      )}
    </dl>
  );
}

function ReportBanner({ report }: { report: ValidationReport }) {
  const blocked = report.migrations.filter((m) => m.phase === 'blocked').length;
  const warnings = report.migrations.filter((m) => m.findings.some((f) => f.severity === 'warning')).length;
  const ready = report.migrations.filter((m) => m.phase === 'ready').length;
  const ok = report.ok && blocked === 0;
  return (
    <div
      role="status"
      className={cn(
        'mb-4 flex items-start gap-2 rounded-md border p-3 text-sm',
        ok ? 'border-status-success/40 bg-status-success/10' : 'border-status-warning/40 bg-status-warning/10',
      )}
    >
      {ok ? (
        <CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-status-success" />
      ) : (
        <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
      )}
      <p className="text-foreground">
        Validation finished: {ready} ready, {blocked} blocked, {warnings} with warnings.{' '}
        {!ok && (
          <a href="#plan-findings" className="underline underline-offset-4">
            Review the findings
          </a>
        )}
      </p>
    </div>
  );
}

export default function PlanDetail() {
  const { planId = '' } = useParams();
  const plan = usePlan(planId);
  const migrations = useMigrations({ plan_id: planId });
  const providers = useProviders();
  const stats = useStats(planId);
  const role = useRole();
  const action = usePlanAction(planId);
  const waveSizeId = useId();
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [waveSize, setWaveSize] = useState('10');
  const [editing, setEditing] = useState(false);
  usePageTitle(plan.data?.name ?? 'Plan');

  const list = useMemo(() => migrations.data ?? [], [migrations.data]);
  const findings = useMemo<FindingRow[]>(
    () => list.flatMap((m) => m.findings.map((f) => ({ ...f, vmName: m.vm.name, migrationId: m.id }))),
    [list],
  );

  if (plan.isPending) return <LoadingBlock label="Loading plan…" rows={6} />;
  if (plan.error || !plan.data) {
    const missing = plan.error instanceof ApiError && plan.error.status === 404;
    return (
      <>
        <PageHeader title="Plan" breadcrumbs={[{ label: 'Plans', to: '/plans' }, { label: planId }]} />
        {missing ? (
          <EmptyState icon={ClipboardX} title={`Plan ${planId} does not exist`} action={<Link to="/plans" className="underline">Back to plans</Link>} />
        ) : (
          <ErrorBanner error={plan.error} title="The plan is unavailable" onRetry={() => void plan.refetch()} />
        )}
      </>
    );
  }

  const p = plan.data;
  const actions = planActions(p, role, list);
  const pending = (key: PlanActionRequest['action']) => action.isPending && action.variables?.action === key;
  const run = (request: PlanActionRequest, after?: (result: unknown) => void) =>
    action.mutate(request, {
      onSuccess: (result) => {
        setConfirm(null);
        after?.(result);
      },
    });
  const waveSizeNumber = Number(waveSize);
  const waveSizeValid = Number.isInteger(waveSizeNumber) && waveSizeNumber >= 1 && waveSizeNumber <= 100;
  const s = stats.data;

  return (
    <>
      <PageHeader
        title={p.name}
        breadcrumbs={[{ label: 'Plans', to: '/plans' }, { label: p.name }]}
        description={p.description ?? undefined}
        meta={
          <>
            <PlanStatusBadge status={p.status} size="md" />
            <code className="text-xs text-muted-foreground">{p.id}</code>
            <span className="text-xs text-muted-foreground">updated {formatRelative(p.updated_at)}</span>
          </>
        }
        actions={
          <div role="group" aria-label="Plan actions" className="flex flex-wrap gap-2">
            <Button size="lg" icon={Pencil} disabledReason={actions.edit.reason} onClick={() => setEditing(true)}>
              Edit plan
            </Button>
            <Button
              size="lg"
              icon={ListChecks}
              loading={pending('validate')}
              disabledReason={actions.validate.reason}
              onClick={() => run({ action: 'validate' }, (r) => setReport(r as ValidationReport))}
            >
              Validate
            </Button>
            <Button size="lg" icon={Rows3} disabledReason={actions.waves.reason} onClick={() => setConfirm('waves')}>
              Auto-plan waves
            </Button>
            <Button size="lg" variant="primary" icon={Play} disabledReason={actions.start.reason} onClick={() => setConfirm('start')}>
              {p.status === 'paused' ? 'Resume' : 'Start'}
            </Button>
            <Button size="lg" icon={Pause} disabledReason={actions.pause.reason} onClick={() => setConfirm('pause')}>
              Pause
            </Button>
          </div>
        }
      />

      {action.error && confirm === null && (
        <ErrorBanner error={action.error} title="The plan action failed" className="mb-4" />
      )}
      {report && <ReportBanner report={report} />}
      {editing && <PlanCreateDialog open onClose={() => setEditing(false)} plan={p} />}

      <section aria-label="Plan metrics" className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
        <KpiTile label="Migrations" value={formatNumber(s?.total ?? list.length)} icon={Layers} hint={`${p.waves.length} wave${p.waves.length === 1 ? '' : 's'}`} />
        <KpiTile label="In progress" value={formatNumber(s?.in_progress ?? 0)} icon={Activity} tone="progress" />
        <KpiTile label="Completed" value={formatNumber(s?.completed ?? 0)} icon={CircleCheck} tone="success" />
        <KpiTile label="Failed" value={formatNumber(s?.failed ?? 0)} icon={CircleX} tone={(s?.failed ?? 0) > 0 ? 'danger' : 'neutral'} />
        <KpiTile label="Avg downtime" value={formatDuration(s?.avg_downtime_s)} icon={Timer} hint={`SLO ${formatDuration(p.downtime_slo_s)}`} />
        <KpiTile label="SLO compliance" value={formatPct(s?.slo_compliance_pct)} icon={Gauge} />
      </section>

      <div className="flex flex-col gap-4">
        <Panel title="Settings" description="What the orchestrator will do for every VM in this plan">
          <SettingsSummary plan={p} providers={providers.data ?? []} />
        </Panel>

        <Panel title="Waves" description="Waves run in order; a wave starts when the waves it depends on are complete">
          <WavesBoard
            plan={p}
            migrations={list}
            emptyAction={
              <Button icon={Rows3} disabledReason={actions.waves.reason} onClick={() => setConfirm('waves')}>
                Auto-plan waves
              </Button>
            }
          />
        </Panel>

        <Panel title="Migrations" description="Strategy, estimated downtime and pre-flight findings per VM">
          {migrations.isPending && <LoadingBlock label="Loading migrations…" rows={5} />}
          {migrations.error && <ErrorBanner error={migrations.error} title="Migrations are unavailable" onRetry={() => void migrations.refetch()} />}
          {migrations.data && <MigrationsTable migrations={list} plan={p} caption={`Migrations in ${p.name}`} />}
        </Panel>

        <section id="plan-findings" tabIndex={-1} className="scroll-mt-20 outline-hidden">
          <Panel
            title="Findings"
            description={`${findings.filter((f) => f.severity === 'blocker').length} blockers, ${findings.filter((f) => f.severity === 'warning').length} warnings, ${findings.filter((f) => f.severity === 'info').length} info`}
          >
            <FindingGroupsList groups={groupFindings(findings)} />
          </Panel>
        </section>
      </div>

      <ConfirmDialog
        open={confirm === 'start'}
        title={p.status === 'paused' ? 'Resume this plan?' : 'Start this plan?'}
        description={
          <>
            {list.length} migrations in {p.waves.length || 1} wave{(p.waves.length || 1) === 1 ? '' : 's'}. Warm migrations begin pre-copy while their
            source VMs keep running. {p.require_approval ? 'Every cutover still needs an approver.' : 'Cutovers do not need approval in this plan.'}
          </>
        }
        confirmLabel={p.status === 'paused' ? 'Resume plan' : 'Start plan'}
        pending={pending('start')}
        error={confirm === 'start' ? action.error : null}
        onConfirm={() => run({ action: 'start' })}
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <ConfirmDialog
        open={confirm === 'pause'}
        title="Pause this plan?"
        description="No new migrations or cutovers will start. Migrations already copying or cutting over finish their current step."
        confirmLabel="Pause plan"
        pending={pending('pause')}
        error={confirm === 'pause' ? action.error : null}
        onConfirm={() => run({ action: 'pause' })}
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <ConfirmDialog
        open={confirm === 'waves'}
        title={p.waves.length ? 'Replace the waves?' : 'Auto-plan waves?'}
        description="Builds a pilot wave of up to three low-risk VMs, then orders the rest by workload tier and disk size; VMs sharing an app tag stay together."
        confirmLabel="Plan waves"
        canConfirm={waveSizeValid}
        pending={pending('waves')}
        error={confirm === 'waves' ? action.error : null}
        onConfirm={() => run({ action: 'waves', body: { max_wave_size: waveSizeNumber } })}
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      >
        <TextField
          id={waveSizeId}
          label="Maximum VMs per wave"
          type="number"
          inputMode="numeric"
          min={1}
          max={100}
          value={waveSize}
          onChange={(e) => setWaveSize(e.target.value)}
          error={waveSizeValid ? null : 'Enter a whole number from 1 to 100.'}
          hint="A wave may exceed this to keep an application together."
        />
      </ConfirmDialog>
    </>
  );
}
