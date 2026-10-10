import { Ban, Bot, CalendarClock, CircleCheck, CircleX, ClipboardX, Hourglass, ShieldAlert, TriangleAlert } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { useEventTail, useMigration, usePlan, useRecommendations, useSetStrategy } from '../api/hooks';
import { useLiveEvents } from '../api/live';
import { useRole } from '../api/session';
import { RECOMMENDATION_LIMIT_MAX, type Event, type GuestOS, type Migration, type Phase, type Plan, type Role, type Strategy, type VerificationConfig } from '../api/types';
import { AdvisorNotes } from '../components/AdvisorNotes';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { CalibrationPanel, ResolvedMappings } from '../components/CalibrationPanel';
import { DowntimeClock } from '../components/DowntimeClock';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { FindingsList } from '../components/FindingsList';
import { MigrationActions } from '../components/MigrationActions';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { PhaseStepper } from '../components/PhaseStepper';
import { ProgressBar } from '../components/ProgressBar';
import { RecommendationActions } from '../components/RecommendationActions';
import { RecommendationList } from '../components/Recommendations';
import { LoadingBlock } from '../components/Skeleton';
import { PhaseBadge } from '../components/StatusBadge';
import { SyncPassChart } from '../components/SyncPassChart';
import { Timeline } from '../components/Timeline';
import { decidable, nonStrategyNotes } from '../lib/advisor';
import { cn } from '../lib/cn';
import { formatBytes, formatDateTime, formatDuration, formatPct, formatRelative } from '../lib/format';
import { guestOsOf, V2V_LABELS } from '../lib/guestOs';
import { clearedByStrategyChange, countedApprovals } from '../lib/migrationActions';
import { isActivePhase, isWarmStrategy, phaseMeta, preflightRan, runningStep } from '../lib/phase';
import { can, hasRole } from '../lib/roles';
import { strategyLabel, STRATEGY_DESCRIPTIONS } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

/** Announces phase changes and every 10 % of progress — never every tick (aria-live polite). */
function useProgressAnnouncement(m: Migration | undefined): string {
  const [text, setText] = useState('');
  const last = useRef<{ id?: string; phase?: Phase; bucket?: number }>({});
  const id = m?.id;
  const phase = m?.phase;
  const pct = m?.progress_pct ?? 0;
  const name = m?.vm.name ?? '';
  useEffect(() => {
    if (!id || !phase) return;
    const bucket = Math.floor(pct / 10);
    if (last.current.id !== id) {
      last.current = { id, phase, bucket };
      return;
    }
    if (last.current.phase !== phase) {
      setText(`${name} is now ${phaseMeta(phase).label}.`);
      last.current = { id, phase, bucket };
    } else if (isActivePhase(phase) && bucket !== last.current.bucket) {
      setText(`${name} ${phaseMeta(phase).label}: ${formatPct(pct, 0)} complete.`);
      last.current.bucket = bucket;
    }
  }, [id, phase, pct, name]);
  return text;
}

/** Phases a retried cutover passes through while its source is still stopped (SDD §5.2). */
const BEFORE_CUTOVER: ReadonlySet<Phase> = new Set(['ready', 'precopy', 'syncing', 'awaiting_cutover']);

function Alerts({ m }: { m: Migration }) {
  const clockOpen = Boolean(m.downtime_started_at) && !m.downtime_ended_at;
  const sourceStopped = m.phase === 'failed' && clockOpen;
  const stillStopped = clockOpen && BEFORE_CUTOVER.has(m.phase);
  return (
    <div className="flex flex-col gap-2 empty:hidden">
      {stillStopped && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-status-danger/40 bg-status-danger/10 p-3 text-sm">
          <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
          <p className="text-foreground">
            <strong className="font-semibold">The source VM is still stopped</strong> since {formatDateTime(m.downtime_started_at)}: the failed
            cutover was retried, so its downtime clock keeps running until the next cutover is verified.
          </p>
        </div>
      )}
      {sourceStopped && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-status-danger/40 bg-status-danger/10 p-3 text-sm">
          <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
          <p className="text-foreground">
            <strong className="font-semibold">The source VM is stopped</strong> since {formatDateTime(m.downtime_started_at)} and the migration failed. Roll back
            to restart it, or fix the cause and retry.
          </p>
        </div>
      )}
      {m.error && (
        <div className="flex items-start gap-2 rounded-md border border-status-danger/40 bg-status-danger/10 p-3 text-sm">
          <CircleX aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
          <p className="wrap-break-word text-foreground">
            <strong className="font-semibold">Last error:</strong> {m.error}
          </p>
        </div>
      )}
      {m.advisor_hold && (
        <div role="status" className="flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm">
          <Hourglass aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          <p className="wrap-break-word text-foreground">
            <strong className="font-semibold">Waiting for advisor approval.</strong> The migration starts once a person approves or overrides the
            advisor&apos;s decision.{' '}
            <Link to={`/advisor/recommendations/${encodeURIComponent(m.advisor_hold)}`} className="underline underline-offset-4">
              See the recommendation
            </Link>
          </p>
        </div>
      )}
      {m.review_required && (
        <div className="flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
          <p className="wrap-break-word text-foreground">
            <strong className="font-semibold">Review required.</strong> {m.review_reason ?? 'The advisor flagged this migration for a human check.'}
          </p>
        </div>
      )}
    </div>
  );
}

function ProgressPanel({ m }: { m: Migration }) {
  const meta = phaseMeta(m.phase);
  const active = isActivePhase(m.phase);
  const step = runningStep(m);
  const transferredId = useId();
  const diskId = useId();
  const stepId = useId();
  return (
    <Panel title="Progress" description={meta.description}>
      <div className="flex flex-col gap-3">
        <div className="flex items-baseline justify-between gap-2">
          <span className="num text-3xl font-semibold text-foreground">{formatPct(m.progress_pct)}</span>
          <PhaseBadge phase={m.phase} />
        </div>
        <ProgressBar value={m.progress_pct} label={`${m.vm.name} ${meta.label} progress`} tone={active ? 'progress' : meta.tone} />
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
          {/* two figures, never one over the other: every pass counts, so a warm migration transfers
              more than its disk holds (SDD §4.2, §16) */}
          <dt id={transferredId} className="text-muted-foreground">
            Transferred
          </dt>
          <dd aria-labelledby={transferredId} className="num text-right">
            {formatBytes(m.bytes_transferred)}
          </dd>
          <dt id={diskId} className="text-muted-foreground">
            Disk used
          </dt>
          <dd aria-labelledby={diskId} className="num text-right">
            {formatBytes(m.bytes_total)}
          </dd>
          {step && (
            <>
              <dt id={stepId} className="text-muted-foreground">
                Current step
              </dt>
              <dd aria-labelledby={stepId} className="num text-right">
                {step.label}
              </dd>
            </>
          )}
          <dt className="text-muted-foreground">Checkpoint</dt>
          <dd className="num text-right">{m.checkpoint ?? '—'}</dd>
          <dt className="text-muted-foreground">Attempts</dt>
          <dd className="num text-right">{m.attempts}</dd>
        </dl>
      </div>
    </Panel>
  );
}

function hasResolvedMappings(m: Migration): boolean {
  const rm = m.resolved_mappings;
  return [rm.flavors, rm.networks, rm.volume_types, rm.projects].some((t) => Object.keys(t).length > 0);
}

function EstimatesPanel({ m, role }: { m: Migration; role: Role | undefined }) {
  const setStrategy = useSetStrategy(m.id);
  const migration = useMigration(m.id);
  // a strategy change clears the migration's approvals and cutover request (SDD §5.4): count them on the
  // server at the click, as the page may predate an approval, and ask first; ask anyway when they cannot
  // be counted
  const [asking, setAsking] = useState<{ strategy: Strategy; cleared: string } | null>(null);
  const [checking, setChecking] = useState<Strategy | null>(null);
  // the outcome, for screen readers, from a live region already on the page (SDD §16)
  const [announcement, setAnnouncement] = useState('');
  const choose = (strategy: Strategy) =>
    setStrategy.mutate(strategy, {
      onSuccess: () => {
        setAsking(null);
        setAnnouncement(`${m.vm.name}: strategy set to ${strategyLabel(strategy)}.`);
      },
    });
  const askOrChoose = async (strategy: Strategy) => {
    setChecking(strategy);
    const latest = await migration.refetch();
    setChecking(null);
    const cleared = latest.data && !latest.isError ? clearedByStrategyChange(latest.data) : 'any approvals and the cutover request';
    if (cleared) setAsking({ strategy, cleared });
    else choose(strategy);
  };
  const canChangePhase = m.phase === 'pending' || m.phase === 'ready' || m.phase === 'blocked';
  const canChangeRole = hasRole(role, 'operator');
  const reasonFor = (strategy: Strategy, eligible: boolean): string | null => {
    if (!canChangeRole) return 'Requires the operator role.';
    if (!canChangePhase) return 'The strategy can change only while pending, ready or blocked.';
    if (!eligible) return 'This strategy is not eligible for this VM.';
    if (strategy === m.strategy) return 'Already selected.';
    return null;
  };
  if (m.estimates.length === 0) return <EmptyState icon={ClipboardX} title="Not estimated yet" description="Validate the plan to estimate every strategy." />;
  return (
    <div className="flex flex-col gap-2">
      <p role="status" className="sr-only">
        {announcement}
      </p>
      {setStrategy.error && asking === null && <ErrorBanner error={setStrategy.error} title="The strategy was not changed" />}
      <div className="table-wrap rounded-md border border-border">
        <table className="data-table">
          <caption className="sr-only">Estimates per strategy</caption>
          <thead>
            <tr>
              <th scope="col">Strategy</th>
              <th scope="col">Eligible</th>
              <th scope="col" className="text-right">
                Downtime
              </th>
              <th scope="col" className="hidden text-right sm:table-cell">
                Pre-copy
              </th>
              <th scope="col" className="hidden text-right md:table-cell">
                Total
              </th>
              <th scope="col">SLO</th>
              <th scope="col">
                <span className="sr-only">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {m.estimates.map((e) => (
              <tr key={e.strategy} className={cn(e.strategy === m.strategy && 'bg-accent/5')}>
                <th scope="row" className="whitespace-nowrap" title={STRATEGY_DESCRIPTIONS[e.strategy]}>
                  {strategyLabel(e.strategy)}
                  {e.strategy === m.strategy && <span className="block text-xs font-normal text-status-success">in use</span>}
                </th>
                <td>
                  {e.eligible ? (
                    <span className="inline-flex items-center gap-1 text-status-success">
                      <CircleCheck aria-hidden className="size-3.5" />
                      Yes
                    </span>
                  ) : (
                    <span className="inline-flex items-start gap-1 text-status-danger" title={e.reasons.join('; ')}>
                      <Ban aria-hidden className="mt-0.5 size-3.5 shrink-0" />
                      <span>
                        No
                        <span className="block max-w-56 text-xs text-muted-foreground">{e.reasons.join('; ')}</span>
                      </span>
                    </span>
                  )}
                </td>
                <td className="num whitespace-nowrap text-right">{formatDuration(e.downtime_s)}</td>
                <td className="num hidden whitespace-nowrap text-right sm:table-cell">
                  {e.passes > 0 ? `${formatDuration(e.precopy_s)} · ${e.passes}×` : '—'}
                </td>
                <td className="num hidden whitespace-nowrap text-right md:table-cell">{formatDuration(e.total_s)}</td>
                <td className="whitespace-nowrap">
                  {e.meets_slo ? (
                    <span className="inline-flex items-center gap-1 text-status-success">
                      <CircleCheck aria-hidden className="size-3.5" />
                      Meets
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-status-warning">
                      <TriangleAlert aria-hidden className="size-3.5" />
                      Misses
                    </span>
                  )}
                </td>
                <td>
                  <Button
                    size="sm"
                    disabledReason={reasonFor(e.strategy, e.eligible)}
                    loading={(setStrategy.isPending && setStrategy.variables === e.strategy) || checking === e.strategy}
                    onClick={() => void askOrChoose(e.strategy)}
                    aria-label={`Use ${strategyLabel(e.strategy)}`}
                  >
                    Use
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ConfirmDialog
        open={asking !== null}
        title={`Change the strategy to ${asking ? strategyLabel(asking.strategy) : ''}?`}
        description={`Changing the strategy clears ${asking?.cleared ?? 'nothing'}: approvers approve ${m.vm.name} again for the new strategy.`}
        confirmLabel="Change and clear"
        pending={setStrategy.isPending}
        error={asking !== null ? setStrategy.error : null}
        onConfirm={() => asking && choose(asking.strategy)}
        onCancel={() => {
          setAsking(null);
          setStrategy.reset();
        }}
      />
    </div>
  );
}

/** How verification will judge this guest (SDD §7.5): the Windows profile or the Linux one. */
function verificationProfile(family: GuestOS['family'], v: VerificationConfig | undefined): string {
  if (!v) return '';
  if (family === 'windows') {
    return v.windows_tcp_ports?.length
      ? `Verified on TCP ${v.windows_tcp_ports.join(', ')}, without the console check`
      : 'Verified without a TCP probe or console check: set Windows ports in the plan';
  }
  const tcp = v.tcp_ports.length ? `TCP ${v.tcp_ports.join(', ')} and ` : '';
  return `Verified on ${tcp}the console log`;
}

/** This migration's recommendations, listed like the Advisor feed, and its other notes (SDD §14.4, §16). */
function MigrationAdvisor({ m }: { m: Migration }) {
  const recommendations = useRecommendations({ migration_id: m.id, limit: RECOMMENDATION_LIMIT_MAX });
  const role = useRole();
  const [decided, setDecided] = useState('');
  const notes = nonStrategyNotes(m.advisor_notes);
  if (recommendations.isPending) return <LoadingBlock rows={3} />;
  const page = recommendations.data;
  if (page && page.items.length === 0 && notes.length === 0) {
    return <EmptyState icon={Bot} title="No advisor recommendations or notes for this migration." />;
  }
  return (
    <div className="flex flex-col gap-4">
      <p role="status" aria-live="polite" className="sr-only">
        {decided}
      </p>
      {recommendations.error ? (
        <ErrorBanner error={recommendations.error} title="Recommendations are unavailable" onRetry={() => void recommendations.refetch()} />
      ) : (
        page &&
        page.items.length > 0 && (
          <div>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recommendations</h3>
            <RecommendationList
              items={page.items}
              showSubject={false}
              actionsFor={(rec, focusAfterRef) =>
                can(role, 'advisor.approve') && decidable(rec, m, page.items.find((r) => !r.legacy)?.id) ? (
                  <RecommendationActions rec={rec} migration={m} focusAfterRef={focusAfterRef} onDecided={setDecided} />
                ) : null
              }
            />
            {page.total > page.items.length && (
              <p className="mt-2 text-xs text-muted-foreground">
                The newest {page.items.length} of {page.total} are shown.
              </p>
            )}
          </div>
        )
      )}
      {notes.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Notes</h3>
          <AdvisorNotes notes={notes} />
        </div>
      )}
    </div>
  );
}

function VmDetails({ m, plan }: { m: Migration; plan?: Plan }) {
  const vm = m.vm;
  const osId = useId();
  const guest = guestOsOf(vm);
  const vmware = m.strategy === 'vmware_cold' || m.strategy === 'vmware_warm';
  return (
    <div className="flex flex-col gap-3 text-sm">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
        <dt className="text-muted-foreground">Source id</dt>
        <dd className="min-w-0">
          <code className="text-xs">{vm.source_id}</code>
        </dd>
        <dt className="text-muted-foreground">Project</dt>
        <dd>{vm.project ?? '—'}</dd>
        <dt className="text-muted-foreground">Flavor</dt>
        <dd>
          {vm.flavor ?? '—'} <span className="num text-muted-foreground">({vm.vcpus} vCPU, {formatBytes(vm.ram_mb * 1024 * 1024)})</span>
        </dd>
        <dt id={osId} className="text-muted-foreground">Guest OS</dt>
        <dd aria-labelledby={osId} title={vm.os_type ?? undefined}>
          {guest.label}
          {guest.lifecycle === 'legacy' && <span className="text-status-warning">, out of vendor support</span>}
          <span className="block text-xs text-muted-foreground">{verificationProfile(guest.family, plan?.verification)}</span>
          {vmware && guest.v2v !== 'supported' && <span className="block text-xs text-status-warning">{V2V_LABELS[guest.v2v]}</span>}
        </dd>
        <dt className="text-muted-foreground">Power</dt>
        <dd>{vm.power_state}</dd>
        <dt className="text-muted-foreground">Size</dt>
        <dd className="num">
          {formatBytes(vm.used_bytes)} used of {formatBytes(vm.disk_bytes)}
        </dd>
        <dt className="text-muted-foreground">Destination</dt>
        <dd className="min-w-0">{m.destination_server_id ? <code className="text-xs">{m.destination_server_id}</code> : 'Not created yet'}</dd>
      </dl>
      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Disks</h3>
        <ul className="flex flex-col gap-1">
          {vm.disks.map((d) => (
            <li key={d.id} className="flex flex-wrap items-baseline justify-between gap-x-2 rounded-sm border border-border px-2 py-1">
              <span className="font-mono text-xs">
                {d.device ?? d.name ?? d.id} {d.bootable && <span className="text-muted-foreground">(boot)</span>}
              </span>
              <span className="num text-xs text-muted-foreground">
                {d.size_gb} GiB · {d.kind}
                {d.volume_type ? ` · ${d.volume_type}` : ''}
                {d.multiattach ? ' · multi-attach' : ''}
                {d.encrypted ? ' · encrypted' : ''}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Networks</h3>
        <ul className="flex flex-col gap-1">
          {vm.nics.map((n, i) => (
            <li key={`${n.network}-${i}`} className="flex flex-wrap justify-between gap-x-2 rounded-sm border border-border px-2 py-1 text-xs">
              <span className="font-mono">{n.network}</span>
              <span className="num text-muted-foreground">
                {n.fixed_ips.join(', ') || 'no fixed IP'} · MTU {n.mtu ?? '—'} · {n.vnic_type}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function When({ at }: { at: string }) {
  return (
    <time dateTime={at} title={formatDateTime(at)} className="text-muted-foreground">
      {formatRelative(at)}
    </time>
  );
}

/** The cutover request and the approvals in time order, saying which approval does not count (SDD §5.4, §16). */
function Approvals({ m, plan }: { m: Migration; plan: Plan | null | undefined }) {
  const counted = new Set(countedApprovals(m, plan));
  // a cutover request shows whatever the approval policy, with its window bypass (SDD §16)
  const request = m.cutover_requested ? (
    <div className="flex flex-col gap-0.5 text-sm">
      <p className="inline-flex flex-wrap items-center gap-1">
        <CircleCheck aria-hidden className="size-3.5 shrink-0 text-status-success" />
        {/* a request stored before ADV-17 names nobody */}
        <strong className="font-medium text-foreground">{m.cutover_requested_by ? `Requested by ${m.cutover_requested_by}` : 'Cutover requested'}</strong>
        {m.cutover_requested_at && <When at={m.cutover_requested_at} />}
      </p>
      {m.force_window && (
        <p className="inline-flex items-center gap-1 text-xs text-status-warning">
          <CalendarClock aria-hidden className="size-3.5 shrink-0" />
          An approver allowed it to start outside the cutover window.
        </p>
      )}
    </div>
  ) : null;
  const entries = [
    ...m.approvals.map((a, i) => ({
      key: `approval-${i}`,
      at: a.at,
      node: (
        <div className="text-sm">
          <p className="inline-flex flex-wrap items-center gap-1">
            <strong className="font-medium text-foreground">Approved by {a.actor}</strong>
            <When at={a.at} />
          </p>
          {a.comment && <p className="wrap-break-word text-muted-foreground">“{a.comment}”</p>}
          {plan?.require_approval !== false && !counted.has(a) && <p className="text-xs text-status-warning">Does not count: {a.actor} requested the cutover.</p>}
        </div>
      ),
    })),
    ...(request ? [{ key: 'request', at: m.cutover_requested_at ?? '', node: request }] : []),
  ].sort((a, b) => (a.at && b.at ? Date.parse(a.at) - Date.parse(b.at) : a.at ? -1 : 1));
  return (
    <div className="flex flex-col gap-2">
      {m.approvals.length === 0 && (
        <p className="text-sm text-muted-foreground">{plan?.require_approval === false ? 'This plan does not require approval.' : 'No approvals yet.'}</p>
      )}
      {entries.length > 0 && (
        <ul className="flex flex-col gap-2">
          {entries.map((e) => (
            <li key={e.key}>{e.node}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function MigrationDetail() {
  const { migrationId = '' } = useParams();
  const migration = useMigration(migrationId);
  const m = migration.data;
  const plan = usePlan(m?.plan_id);
  const history = useEventTail({ migration_id: migrationId }, 300);
  const role = useRole();
  const [liveEvents, setLiveEvents] = useState<Event[]>([]);
  usePageTitle(m ? `${m.vm.name} · ${phaseMeta(m.phase).label}` : 'Migration');
  const announcement = useProgressAnnouncement(m);

  useEffect(() => setLiveEvents([]), [migrationId]);
  useLiveEvents((event) => {
    if (event.migration_id === migrationId && event.seq > 0) setLiveEvents((list) => [...list, event].slice(-300));
  });
  const events = useMemo(() => {
    const bySeq = new Map<number, Event>();
    for (const e of history.data ?? []) bySeq.set(e.seq, e);
    for (const e of liveEvents) bySeq.set(e.seq, e);
    return [...bySeq.values()];
  }, [history.data, liveEvents]);

  if (migration.isPending) return <LoadingBlock label="Loading migration…" rows={8} />;
  if (migration.error || !m) {
    const missing = migration.error instanceof ApiError && migration.error.status === 404;
    return (
      <>
        <PageHeader title="Migration" breadcrumbs={[{ label: 'Plans', to: '/plans' }, { label: migrationId }]} />
        {missing ? (
          <EmptyState icon={ClipboardX} title={`Migration ${migrationId} does not exist`} action={<Link to="/plans" className="underline">Back to plans</Link>} />
        ) : (
          <ErrorBanner error={migration.error} title="The migration is unavailable" onRetry={() => void migration.refetch()} />
        )}
      </>
    );
  }

  const p = plan.data;
  // SDD §16: without its plan the cutover window and approval policy are unknown
  const planUnavailable = Boolean(plan.error) && !p;
  const warm = isWarmStrategy(m.strategy);
  const wave = p?.waves.find((w) => w.id === m.wave_id);

  return (
    <>
      <PageHeader
        title={<span className="font-mono">{m.vm.name}</span>}
        breadcrumbs={[{ label: 'Plans', to: '/plans' }, { label: p?.name ?? m.plan_id, to: `/plans/${m.plan_id}` }, { label: m.vm.name }]}
        meta={
          <>
            <PhaseBadge phase={m.phase} size="md" />
            <span className="text-sm text-foreground">{strategyLabel(m.strategy)}</span>
            {wave && <span className="text-sm text-muted-foreground">Wave {wave.order}: {wave.name}</span>}
            <code className="text-xs text-muted-foreground">{m.id}</code>
          </>
        }
      />
      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      <div className="flex flex-col gap-4">
        {planUnavailable && (
          <ErrorBanner error={plan.error} title="The plan of this migration is unavailable" onRetry={() => void plan.refetch()} />
        )}
        <Alerts m={m} />

        <Panel title="Next step">
          <MigrationActions migration={m} plan={p} role={role} planUnavailable={planUnavailable} />
        </Panel>

        <section aria-label="Lifecycle" className="card p-4">
          <PhaseStepper migration={m} />
        </section>

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <ProgressPanel m={m} />
          <Panel title="Downtime" description="From source stop to verified boot on RHOSO">
            <DowntimeClock
              startedAt={m.downtime_started_at}
              endedAt={m.downtime_ended_at}
              actualS={m.actual_downtime_s}
              sloS={p?.downtime_slo_s ?? null}
              estimateS={m.estimate?.downtime_s ?? null}
            />
          </Panel>
          <Panel
            title="Approvals"
            description={p?.require_approval ? (p.require_distinct_approver ? 'An approver other than the requester must approve the cutover' : 'An approver must approve the cutover') : undefined}
          >
            <Approvals m={m} plan={p} />
          </Panel>
        </div>

        {warm && (
          <Panel title="Sync-pass convergence" description="Bytes changed per pass; cutover becomes possible once a delta is below the threshold">
            <SyncPassChart
              passes={m.sync_passes}
              thresholdBytes={p?.convergence_threshold_bytes ?? null}
              maxPasses={p?.max_sync_passes ?? null}
              droppedBytes={m.sync_bytes_dropped}
              running={runningStep(m)?.pass ?? null}
            />
          </Panel>
        )}

        <div className="grid gap-4 xl:grid-cols-2">
          <Panel title="Findings" description="Pre-flight checks (SDD §9.3)">
            <FindingsList
              findings={m.findings}
              emptyText={
                m.phase === 'validating'
                  ? 'No findings yet — pre-flight is running.'
                  : preflightRan(m)
                    ? undefined
                    : 'No findings yet — pre-flight runs when the plan is validated.'
              }
            />
          </Panel>
          <Panel title="Advisor" description="What the advisor recommended, what was decided, and its notes">
            <MigrationAdvisor m={m} />
          </Panel>
        </div>

        <Panel title="Estimates" description="Every strategy considered for this VM (SDD §9.1)">
          <EstimatesPanel m={m} role={role} />
        </Panel>

        {/* the short panels stack beside the long timeline instead of leaving half rows empty */}
        <div className="grid items-start gap-4 xl:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-4">
            <Panel title="Estimate inputs" description="What the downtime estimate rests on; delta passes calibrate it (SDD §9.1)">
              <CalibrationPanel migration={m} plan={p} />
            </Panel>
            {hasResolvedMappings(m) && (
              <Panel title="Resolved mappings" description="Matched automatically by pre-flight; add a plan mapping to override (SDD §9.3)">
                <ResolvedMappings migration={m} />
              </Panel>
            )}
            <Panel title="VM">
              <VmDetails m={m} plan={p} />
            </Panel>
          </div>
          <Panel title="Timeline" description="Phase changes and audit events, newest first">
            {history.error && <ErrorBanner error={history.error} title="Events are unavailable" onRetry={() => void history.refetch()} />}
            <Timeline history={m.phase_history} events={events} />
          </Panel>
        </div>
      </div>
    </>
  );
}
