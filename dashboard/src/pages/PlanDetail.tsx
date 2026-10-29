import {
  Activity,
  BadgeCheck,
  Ban,
  CircleCheck,
  CircleX,
  ClipboardX,
  Gauge,
  History,
  Hourglass,
  Layers,
  ListChecks,
  Pause,
  Pencil,
  Play,
  Rows3,
  Timer,
  TriangleAlert,
} from 'lucide-react';
import { useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { useMigrationSummaries, usePlan, usePlanAction, usePolicies, useProviders, useStats, type PlanActionRequest } from '../api/hooks';
import { useRole } from '../api/session';
import type { AdvisorPolicy, Plan, Provider, ValidationReport } from '../api/types';
import { AdvisorApprovals } from '../components/AdvisorApprovals';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { PlanCreateDialog } from '../components/PlanCreateDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { TextField } from '../components/Field';
import { FinalizePlanDialog, FinalizeQueue, FinalizeSkippedNotice, type FinalizeScope } from '../components/FinalizeQueue';
import { FindingGroupsList } from '../components/FindingsList';
import { KpiTile } from '../components/KpiTile';
import { MigrationsTable } from '../components/MigrationsTable';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { SectionNav } from '../components/SectionNav';
import { LoadingBlock } from '../components/Skeleton';
import { ProbeWarnings } from '../components/ProbeWarnings';
import { PlanStatusBadge } from '../components/StatusBadge';
import { TechPreviewBadge } from '../components/TechPreviewBadge';
import { Unavailable } from '../components/Unavailable';
import { PlanNotificationsPanel } from '../components/Subscriptions';
import { WavesBoard } from '../components/WavesBoard';
import { DECISION_TYPE_LABELS, thresholdDecimals } from '../lib/advisor';
import { cn } from '../lib/cn';
import { finalizedText, finalizeQueue, isOverdue, skippedVms, type SkippedVm } from '../lib/finalize';
import { formatBytes, formatDateTime, formatDuration, formatNumber, formatPct, formatRate, formatRelative } from '../lib/format';
import { clearedByValidation } from '../lib/migrationActions';
import { resolvePolicy } from '../lib/policies';
import { preflightRan } from '../lib/phase';
import { planActions } from '../lib/planActions';
import { NOTHING_MIGRATED_NEXT, nothingMigrated } from '../lib/planDone';
import { destinationName, providerLabel } from '../lib/providerUse';
import { leftOutText } from '../lib/stats';
import { PROVIDER_KIND_LABELS, strategyLabel } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';
import { useHashLanding } from '../lib/useHashLanding';
import { usePlanFindings } from '../lib/usePlanFindings';

type Confirm = 'validate' | 'start' | 'pause' | 'waves' | 'cancel' | null;

const PLAN_SECTIONS = [
  ['plan-metrics', 'Summary'],
  ['plan-settings', 'Settings'],
  ['plan-waves', 'Waves'],
  ['plan-migrations', 'Migrations'],
  ['plan-findings', 'Findings'],
] as const;

function Setting({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 wrap-break-word text-sm text-foreground">{children}</dd>
    </div>
  );
}

function MappingList({ title, map, aside }: { title: string; map: Record<string, string>; aside?: ReactNode }) {
  const entries = Object.entries(map);
  if (entries.length === 0) return null;
  return (
    <div>
      <p className="text-xs text-muted-foreground">{title}</p>
      {aside && <div className="my-0.5">{aside}</div>}
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

function providerLink(provider: Provider | undefined, id: string) {
  if (!provider) return <code>{id}</code>;
  return (
    <Link to={`/inventory/${provider.id}`} className="underline underline-offset-4">
      {providerLabel(provider)}
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

/** What the policy in force does with a migration strategy decision (SDD §14.6, §16, ADV-20). */
function policyModeText(policy: AdvisorPolicy): string {
  const what = DECISION_TYPE_LABELS.migration_strategy.toLowerCase();
  return policy.mode === 'always_require_approval'
    ? `Asks a person for every ${what} decision`
    : `Applies a ${what} decision at ${formatPct(policy.threshold * 100, thresholdDecimals(policy.threshold))} confidence or more`;
}

function SettingsSummary({
  plan,
  providers,
  policy,
  builtIn = false,
}: {
  plan: Plan;
  providers: Provider[];
  /** The advisor policy in force; undefined while unknown (loading or not loaded). */
  policy: AdvisorPolicy | undefined;
  /** No stored policy applies: the built-in default decides, named as on /advisor/policies. */
  builtIn?: boolean;
}) {
  const source = providers.find((p) => p.id === plan.source_provider_id);
  const destination = providers.find((p) => p.id === plan.destination_provider_id);
  const overrides = Object.keys(plan.strategy_overrides).length;
  const v = plan.verification;
  // the kinds the Mappings entry lists, its GPU mappings among them (SDD §16, §22.4)
  const { networks, flavors, volume_types, projects, gpus } = plan.mappings;
  const mappingCount = [networks, flavors, volume_types, projects, gpus].reduce((n, m) => n + Object.keys(m ?? {}).length, 0);
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-4">
      <Setting label="Source">
        {providerLink(source, plan.source_provider_id)}
        {source && <span className="block text-xs text-muted-foreground">{PROVIDER_KIND_LABELS[source.kind]}</span>}
      </Setting>
      <Setting label="Destination">{providerLink(destination, plan.destination_provider_id)}</Setting>
      <Setting label="Downtime SLO">
        <span className="num">{formatDuration(plan.downtime_slo_s)}</span> per VM
      </Setting>
      <Setting label="Strategy">
        {strategyLabel(plan.default_strategy)} · {plan.selection_policy === 'min_downtime' ? 'minimum downtime' : 'simplest meeting the SLO'}
        {overrides > 0 && <span className="block text-xs text-muted-foreground">{overrides} per-VM override(s)</span>}
      </Setting>
      {/* the two kinds of approval, each named (SDD §16, ADV-20) */}
      <Setting label="Cutover approval">
        {plan.require_approval
          ? plan.require_distinct_approver
            ? 'Yes — an approver other than the requester must approve each cutover'
            : 'Yes — an approver must approve each cutover'
          : 'No'}
      </Setting>
      <Setting label="Advisor policy">
        {policy ? (
          <>
            <Link to={`/advisor/policies#policy-${encodeURIComponent(policy.id)}`} className="underline underline-offset-4">
              {builtIn ? 'Built-in default' : policy.name}
            </Link>
            <span className="block text-xs text-muted-foreground">
              {policyModeText(policy)}
              {plan.default_strategy !== 'auto' && '; not asked while the plan sets the strategy'}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">Unknown</span>
        )}
      </Setting>
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
      {plan.port_creation_mode === 'neutron' && <Setting label="Ports">MAC and fixed IPs kept</Setting>}
      {plan.extent_tracking && (
        <Setting label="Changed extents">
          {/* an extent element: its Technology Preview badge (SDD §16, §28.11, plan task CE10) */}
          <span className="inline-flex flex-wrap items-center gap-2">
            Tracked on the source Ceph cluster <TechPreviewBadge feature="extents" />
          </span>
        </Setting>
      )}
      {Object.keys(plan.nic_overrides ?? {}).length > 0 && (
        <Setting label="Network overrides">
          {Object.keys(plan.nic_overrides).length === 1 ? '1 VM' : `${Object.keys(plan.nic_overrides).length} VMs`}
        </Setting>
      )}
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
                {/* a GPU element: its Technology Preview badge (SDD §16, §22.11) */}
                <MappingList title="GPUs" map={gpus ?? {}} aside={<TechPreviewBadge />} />
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
  // summaries: what its lists read, not every migration whole (SDD §16, perf audit PF-02)
  const migrations = useMigrationSummaries({ plan_id: planId });
  const providers = useProviders();
  const stats = useStats(planId);
  const policies = usePolicies();
  const role = useRole();
  const action = usePlanAction(planId);
  const waveSizeId = useId();
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [waveSize, setWaveSize] = useState('10');
  const [editing, setEditing] = useState(false);
  const [clearing, setClearing] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [finalizing, setFinalizing] = useState<FinalizeScope | null>(null);
  // the outcome of Start, Resume, Pause and Auto-plan waves, for screen readers (SDD §16)
  const [announcement, setAnnouncement] = useState('');
  // what the last bulk finalize skipped, shown until dismissed (SDD §16)
  const [notFinalized, setNotFinalized] = useState<SkippedVm[]>([]);
  // the header's Validate and Auto-plan waves stay on the page: they take the focus when the empty
  // state an action was started from goes away with it (SDD §16)
  const validateRef = useRef<HTMLButtonElement>(null);
  const wavesRef = useRef<HTMLButtonElement>(null);
  const emptyValidateRef = useRef<HTMLButtonElement>(null);
  // the plan's heading takes the focus when an action takes its button with it — Start, Resume, Finalize — read
  // by the dialog as it closes: empty on Cancel, so focus goes back to the button (SDD §16)
  const headingRef = useRef<HTMLHeadingElement>(null);
  const afterActionRef = useRef<HTMLElement | null>(null);
  usePageTitle(plan.data?.name ?? 'Plan');
  useHashLanding(Boolean(plan.data) && !plan.error);

  const list = useMemo(() => migrations.data ?? [], [migrations.data]);
  const { rows: findings, groups: findingGroups } = usePlanFindings(list);

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
  const validate = () =>
    run({ action: 'validate' }, (r) => {
      setReport(r as ValidationReport);
      if (document.activeElement === emptyValidateRef.current) validateRef.current?.focus();
    });
  // a re-validation clears the approvals and cutover requests of migrations that have not started (SDD §5.4):
  // count them on the server at the click, as a list loaded before an approval would hide it; ask anyway
  // when they cannot be counted
  const askOrValidate = async () => {
    setChecking(true);
    const latest = await migrations.refetch();
    setChecking(false);
    const cleared = latest.data && !latest.isError ? clearedByValidation(latest.data) : 'any approvals and cutover requests';
    if (!cleared) {
      validate();
      return;
    }
    setClearing(cleared);
    setConfirm('validate');
  };
  // "pre-flight passed" only once every VM of vm_ids has a migration whose pre-flight ran and the plan is
  // not a draft again: an added VM has no migration before the next validation (SDD §16)
  const checked = new Set(list.filter(preflightRan).map((m) => m.vm.source_id));
  const preflightDone = p.status !== 'draft' && p.vm_ids.length > 0 && p.vm_ids.every((vmId) => checked.has(vmId));
  const waveSizeNumber = Number(waveSize);
  // the API's bound (SDD §12)
  const waveSizeValid = Number.isInteger(waveSizeNumber) && waveSizeNumber >= 1 && waveSizeNumber <= 1000;
  const s = stats.data;
  // what is built from the migrations reads as unknown when they could not be loaded, never as empty (SDD §16)
  const migrationsUnknown = Boolean(migrations.error) && !migrations.data;
  const migrationCount = migrations.data?.length ?? s?.total;
  // the primary action follows the derived status (SDD §8.1, ADV-19): Start is hidden once it has nothing to
  // start — while the plan runs (Pause is its action) and once its migrations ended; a Completed plan finalizes
  const ended = p.derived_status === 'completed' || p.derived_status === 'finalized';
  // every migration it did not cancel rolled back: Completed, and no success (SDD §8.1 rule 6, §16)
  const migratedNothing = nothingMigrated(p);
  // the advisor policy in force for the plan's strategy decisions (SDD §14.6, §16, ADV-20)
  const stored = policies.data ? resolvePolicy(policies.data.items, p.id, p.source_provider_id, 'migration_strategy') : null;
  const policy = policies.data ? { policy: stored ?? policies.data.default, builtIn: stored === null } : undefined;
  const startShown = p.status !== 'running' && !ended;
  // a Paused plan resumes, also one validated again while its migrations are at work (SDD §8.1)
  const resumes = p.derived_status === 'paused';
  const askStart = () => {
    afterActionRef.current = null;
    setConfirm('start');
  };
  const askCancel = () => {
    afterActionRef.current = null;
    setConfirm('cancel');
  };
  // whether a migration started is unknown while the migrations cannot be loaded: the API checks anyway (SDD §12)
  const cancelReason = migrationsUnknown ? 'The migrations could not be loaded: whether one has started is unknown.' : actions.cancel.reason;
  const unstarted = list.filter((m) => m.phase !== 'cancelled').length;
  const askFinalize = (scope: FinalizeScope) => {
    afterActionRef.current = null;
    setFinalizing(scope);
  };
  const overdue = finalizeQueue(list).filter((m) => isOverdue(m, p.finalize_warning_h)).length;

  return (
    <>
      <PageHeader
        title={p.name}
        titleRef={headingRef}
        breadcrumbs={[{ label: 'Plans', to: '/plans' }, { label: p.name }]}
        description={p.description ?? undefined}
        meta={
          <>
            <PlanStatusBadge status={p.derived_status} size="md" nothingMigrated={migratedNothing} />
            <code className="text-xs text-muted-foreground">{p.id}</code>
            <span className="text-xs text-muted-foreground">updated {formatRelative(p.updated_at)}</span>
          </>
        }
        actions={
          // a cancelled plan offers no action but its history (SDD §12, §16)
          p.status !== 'cancelled' && (
          <div role="group" aria-label="Plan actions" className="flex flex-wrap gap-2">
            <Button size="lg" icon={Pencil} disabledReason={actions.edit.reason} onClick={() => setEditing(true)}>
              Edit plan
            </Button>
            <Button
              ref={validateRef}
              size="lg"
              icon={ListChecks}
              loading={pending('validate') || checking}
              disabledReason={actions.validate.reason}
              onClick={() => void askOrValidate()}
            >
              Validate
            </Button>
            <Button ref={wavesRef} size="lg" icon={Rows3} disabledReason={actions.waves.reason} onClick={() => setConfirm('waves')}>
              Auto-plan waves
            </Button>
            {p.derived_status === 'completed' ? (
              // nothing awaits finalize where nothing was migrated: the next step is each VM's own (SDD §16)
              !migratedNothing && (
                <Button size="lg" variant="primary" icon={BadgeCheck} disabledReason={actions.finalize.reason} onClick={() => askFinalize({ wave: null })}>
                  Finalize all
                </Button>
              )
            ) : (
              startShown && (
                <Button size="lg" variant="primary" icon={Play} disabledReason={actions.start.reason} onClick={askStart}>
                  {resumes ? 'Resume' : p.status === 'failed' ? 'Start again' : 'Start'}
                </Button>
              )
            )}
            {!ended && (
              <Button size="lg" icon={Pause} disabledReason={actions.pause.reason} onClick={() => setConfirm('pause')}>
                Pause
              </Button>
            )}
            <Button size="lg" variant="ghost" icon={Ban} disabledReason={cancelReason} onClick={askCancel}>
              Cancel plan
            </Button>
          </div>
          )
        }
      />

      {action.error && confirm === null && (
        <ErrorBanner error={action.error} title="The plan action failed" className="mb-4" />
      )}
      {/* a live region already on the page: one inserted together with its text is not reliably read (SDD §16) */}
      <div role="status">{report && <ReportBanner report={report} />}</div>
      <p role="status" className="sr-only">
        {announcement}
      </p>
      {editing && (
        <PlanCreateDialog
          open
          onClose={() => setEditing(false)}
          plan={p}
          failedVms={list.filter((m) => m.phase === 'failed').map((m) => m.vm.name)}
          onSaved={() => setAnnouncement('Plan saved. It is a draft again: validate it before starting.')}
        />
      )}

      {stats.error && <ErrorBanner error={stats.error} title="Statistics are unavailable" onRetry={() => void stats.refetch()} className="mb-4" />}
      {migratedNothing && (
        // a Completed plan that migrated nothing says so, with the next step (SDD §16)
        <div role="note" aria-label="Nothing was migrated" className="mb-4 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm">
          <History aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          <p className="text-foreground">{NOTHING_MIGRATED_NEXT}</p>
        </div>
      )}
      <SectionNav label="Plan sections" sections={PLAN_SECTIONS} />

      {/* a figure the statistics did not deliver is unknown (—), never a zero (SDD §16) */}
      <section id="plan-metrics" tabIndex={-1} aria-label="Plan metrics" className="mb-4 grid scroll-mt-20 grid-cols-2 gap-3 outline-offset-2 focus:outline-2 focus:outline-ring md:grid-cols-4 2xl:grid-cols-7">
        <KpiTile label="Migrations" value={formatNumber(s?.total ?? migrations.data?.length)} icon={Layers} hint={`${p.waves.length} wave${p.waves.length === 1 ? '' : 's'}`} />
        <KpiTile label="In progress" value={formatNumber(s?.in_progress)} icon={Activity} tone="progress" />
        {/* Completed, split: verified with the source VM kept, and finalized (SDD §4.2, §16, ADV-19) */}
        <KpiTile
          label="Awaiting finalize"
          value={formatNumber(s?.awaiting_finalize)}
          icon={Hourglass}
          tone={overdue > 0 ? 'warning' : 'neutral'}
          hint={overdue > 0 ? `${overdue} overdue` : undefined}
        />
        <KpiTile label="Finalized" value={formatNumber(s?.finalized)} icon={BadgeCheck} tone="success" />
        <KpiTile label="Failed" value={formatNumber(s?.failed)} icon={CircleX} tone={(s?.failed ?? 0) > 0 ? 'danger' : 'neutral'} />
        {/* a migration whose numbers do not hold is counted, not measured (SDD §5.5) */}
        <KpiTile label="Avg downtime" value={formatDuration(s?.avg_downtime_s)} icon={Timer} hint={[`SLO ${formatDuration(p.downtime_slo_s)}`, leftOutText(s)].filter(Boolean).join('; ')} />
        <KpiTile label="SLO compliance" value={formatPct(s?.slo_compliance_pct)} icon={Gauge} hint={leftOutText(s) ?? undefined} />
      </section>

      <div className="flex flex-col gap-4">
        <AdvisorApprovals planId={p.id} migrations={migrations.data ?? []} onDecided={setAnnouncement} />
        {notFinalized.length > 0 && (
          <FinalizeSkippedNotice
            skipped={notFinalized}
            onDismiss={() => {
              setNotFinalized([]);
              headingRef.current?.focus();
            }}
          />
        )}
        <FinalizeQueue plan={p} destination={destinationName(providers.data, p)} migrations={list} gate={actions.finalize} allInHeader={p.derived_status === 'completed'} onFinalize={askFinalize} />
        <ProbeWarnings plan={p} migrations={migrations.data ?? []} edit={actions.edit} onSaved={setAnnouncement} focusAfterRef={headingRef} />

        <Panel id="plan-settings" title="Settings" description="What the orchestrator will do for every VM in this plan">
          <SettingsSummary plan={p} providers={providers.data ?? []} policy={policy?.policy} builtIn={policy?.builtIn} />
        </Panel>

        <Panel id="plan-waves" title="Waves" description="Waves run in order; a wave starts when the waves it depends on are complete">
          <WavesBoard
            plan={p}
            migrations={migrationsUnknown ? null : list}
            emptyAction={
              <Button icon={Rows3} disabledReason={actions.waves.reason} onClick={() => setConfirm('waves')}>
                Auto-plan waves
              </Button>
            }
          />
        </Panel>

        <Panel id="plan-migrations" title="Migrations" description="Strategy, estimated downtime and pre-flight findings per VM">
          {migrations.isPending && <LoadingBlock label="Loading migrations…" rows={5} />}
          {migrations.error && <ErrorBanner error={migrations.error} title="Migrations are unavailable" onRetry={() => void migrations.refetch()} />}
          {migrations.data && (
            <MigrationsTable
              migrations={list}
              plan={p}
              caption={`Migrations in ${p.name}`}
              emptyDescription="Validation creates one migration per VM and runs the pre-flight checks."
              emptyAction={
                <Button
                  ref={emptyValidateRef}
                  icon={ListChecks}
                  loading={pending('validate') || checking}
                  disabledReason={actions.validate.reason}
                  onClick={() => void askOrValidate()}
                >
                  Validate
                </Button>
              }
            />
          )}
        </Panel>

        <section id="plan-findings" tabIndex={-1} className="scroll-mt-20 outline-offset-2 focus:outline-2 focus:outline-ring">
          <Panel
            title="Findings"
            description={
              migrationsUnknown
                ? undefined
                : `${findings.filter((f) => f.severity === 'blocker').length} blockers, ${findings.filter((f) => f.severity === 'warning').length} warnings, ${findings.filter((f) => f.severity === 'info').length} info`
            }
          >
            {migrationsUnknown ? (
              <Unavailable what="findings" />
            ) : (
              <FindingGroupsList
                groups={findingGroups}
                emptyText={preflightDone ? undefined : 'No findings yet — pre-flight runs when the plan is validated.'}
              />
            )}
          </Panel>
        </section>

        {/* the channels that follow this plan (SDD §16, §24.4, plan task NT9): not a field of the plan */}
        <PlanNotificationsPanel planId={p.id} />
      </div>

      <ConfirmDialog
        open={confirm === 'start'}
        title={resumes ? 'Resume this plan?' : p.status === 'failed' ? 'Start this plan again?' : 'Start this plan?'}
        description={
          <>
            {p.status === 'failed' &&
              'Pre-staging failed (the events say why): starting again retries it before any migration starts. Failed migrations stay failed until you retry or roll them back. '}
            {migrationCount ?? 'An unknown number of'} migrations in {p.waves.length || 1} wave{(p.waves.length || 1) === 1 ? '' : 's'}. Warm migrations begin pre-copy while their
            source VMs keep running. {p.require_approval ? 'Every cutover still needs an approver.' : 'Cutovers do not need approval in this plan.'}
          </>
        }
        confirmLabel={resumes ? 'Resume plan' : p.status === 'failed' ? 'Start again' : 'Start plan'}
        pending={pending('start')}
        error={confirm === 'start' ? action.error : null}
        returnFocusRef={afterActionRef}
        onConfirm={() =>
          run({ action: 'start' }, () => {
            // a started plan runs: Start goes, Pause is its action
            afterActionRef.current = headingRef.current;
            setAnnouncement(p.status === 'paused' ? 'Plan resumed.' : p.status === 'failed' ? 'Plan started again.' : 'Plan started.');
          })
        }
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <FinalizePlanDialog
        plan={p}
        migrations={list}
        scope={finalizing}
        returnFocusRef={afterActionRef}
        onClose={() => setFinalizing(null)}
        onDone={(result) => {
          // Finalize all and Finalize wave go once their VMs are finalized
          afterActionRef.current = headingRef.current;
          setAnnouncement(finalizedText(result));
          setNotFinalized(skippedVms(result, list));
        }}
      />
      <ConfirmDialog
        open={confirm === 'pause'}
        title="Pause this plan?"
        description="No new migrations or cutovers will start. Migrations already copying or cutting over finish their current step."
        confirmLabel="Pause plan"
        pending={pending('pause')}
        error={confirm === 'pause' ? action.error : null}
        onConfirm={() => run({ action: 'pause' }, () => setAnnouncement('Plan paused.'))}
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <ConfirmDialog
        open={confirm === 'cancel'}
        tone="danger"
        icon={Ban}
        title={`Cancel ${p.name}?`}
        subject={`Plan ${p.id}`}
        consequences={[
          { kind: 'kept', text: 'the plan and its history, shown as Cancelled' },
          {
            kind: 'then',
            text: `its ${unstarted} migration${unstarted === 1 ? ' that has' : 's that have'} not started ${unstarted === 1 ? 'is' : 'are'} cancelled: its VMs can be planned again, and its clouds deleted once no other plan uses them`,
          },
        ]}
        confirmLabel="Cancel plan"
        cancelLabel="Keep plan"
        requireText={p.name}
        pending={pending('cancel')}
        error={confirm === 'cancel' ? action.error : null}
        returnFocusRef={afterActionRef}
        onConfirm={() =>
          run({ action: 'cancel' }, () => {
            // the plan's actions go with it: the focus goes to its heading, never the page (SDD §16)
            afterActionRef.current = headingRef.current;
            setAnnouncement('Plan cancelled.');
          })
        }
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <ConfirmDialog
        open={confirm === 'validate'}
        returnFocusRef={validateRef}
        title="Validate this plan again?"
        description={`Validating again clears ${clearing ?? 'nothing'} of the migrations that have not started: approvers approve them again after the new assessment.`}
        confirmLabel="Validate and clear"
        pending={pending('validate')}
        error={confirm === 'validate' ? action.error : null}
        onConfirm={validate}
        onCancel={() => {
          setConfirm(null);
          action.reset();
        }}
      />
      <ConfirmDialog
        open={confirm === 'waves'}
        returnFocusRef={wavesRef}
        title={p.waves.length ? 'Replace the waves?' : 'Auto-plan waves?'}
        description="Builds a pilot wave of up to three low-risk VMs, then orders the rest by workload tier and disk size; VMs sharing an app tag stay together. The plan returns to draft: validate it again before starting, which clears approvals and cutover requests."
        confirmLabel="Plan waves"
        canConfirm={waveSizeValid}
        confirmReason="Enter a whole number from 1 to 1000."
        pending={pending('waves')}
        error={confirm === 'waves' ? action.error : null}
        onConfirm={() =>
          run({ action: 'waves', body: { max_wave_size: waveSizeNumber } }, (result) => {
            const n = (result as Plan).waves.length;
            setAnnouncement(`Waves planned: ${n} wave${n === 1 ? '' : 's'}.`);
          })
        }
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
          max={1000}
          value={waveSize}
          onChange={(e) => setWaveSize(e.target.value)}
          error={waveSizeValid ? null : 'Enter a whole number from 1 to 1000.'}
          liveError
          hint="A wave may exceed this to keep an application together."
        />
      </ConfirmDialog>
    </>
  );
}
