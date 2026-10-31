import { ClipboardList, Plus, SearchX } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { usePlans, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import { DERIVED_PLAN_STATUSES, type Plan } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { PlanCreateDialog } from '../components/PlanCreateDialog';
import { ProgressBar } from '../components/ProgressBar';
import { LoadingBlock } from '../components/Skeleton';
import { SortableHeader } from '../components/SortableHeader';
import { PlanStatusBadge } from '../components/StatusBadge';
import { formatDateTime, formatDuration, formatNumber, formatRelative } from '../lib/format';
import { providerLabel } from '../lib/providerUse';
import { hasRole } from '../lib/roles';
import { nextSort, sortBy, type SortState } from '../lib/sort';
import { planStatusMeta } from '../lib/status';
import { nothingMigrated, planDone } from '../lib/planDone';
import { usePageTitle } from '../lib/usePageTitle';

type SortKey = 'name' | 'status' | 'vms' | 'done' | 'slo' | 'updated';
const SORT_KEYS: SortKey[] = ['name', 'status', 'vms', 'done', 'slo', 'updated'];

/** Router state used by Inventory to open the dialog with a selection. */
export interface NewPlanState {
  /** `sourceProject`: the project credential the Inventory read the VMs with, the plan's source project (SDD §16). */
  newPlan?: { sourceId: string; vmIds: string[]; sourceProject?: string | null };
}

/** A plan's migrations done, from the counts it carries: the list reads no migration (SDD §16, §8.1). */
const planProgress = (plan: Plan) => planDone(plan, plan.migration_counts ?? {});

/** What a cancelled plan shows where the others show their progress: it has nothing left to do (SDD §16). */
function NothingLeft() {
  return (
    <span className="num shrink-0 text-xs text-muted-foreground">
      <span aria-hidden>—</span>
      <span className="sr-only">Nothing left to do</span>
    </span>
  );
}

/** The cancelled migrations a done count leaves out, on a line of its own: beside the count they squeezed its bar to a dot (SDD §16). */
function CancelledLine({ n, className = '' }: { n: number; className?: string }) {
  return (
    <span className={`mt-0.5 block text-xs font-normal text-muted-foreground ${className}`}>
      {/* read on from the count above it */}
      <span className="sr-only">, </span>
      {n} cancelled
    </span>
  );
}

export default function Plans() {
  usePageTitle('Plans');
  const searchId = useId();
  const plans = usePlans();
  const providers = useProviders();
  const role = useRole();
  const location = useLocation();
  const navigate = useNavigate();
  const searchRef = useRef<HTMLInputElement>(null);
  const [params, setParams] = useSearchParams();
  const query = params.get('q') ?? '';
  // every view shows the status the migrations tell, never the stored lifecycle (SDD §8.1)
  const statusParam = params.get('status');
  const status = DERIVED_PLAN_STATUSES.find((value) => value === statusParam) ?? 'all';
  const sort = useMemo<SortState<SortKey>>(() => ({
    key: SORT_KEYS.find((value) => value === params.get('sort')) ?? 'updated',
    direction: params.get('direction') === 'ascending' ? 'ascending' : 'descending',
  }), [params]);
  const changeParams = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };
  const [dialog, setDialog] = useState<{ open: boolean; sourceId?: string; vmIds?: string[]; sourceProject?: string | null }>({ open: false });
  const canCreate = hasRole(role, 'operator');

  // Opened from Inventory with a VM selection: taken from the navigation's state as the page renders, the state then
  // cleared, and the dialog opened as the page renders without it - a render after the page's first, as when an effect
  // opened it, so the layout has moved the focus to the page by then and the dialog gives it back there
  const offered = canCreate ? (location.state as NewPlanState | null)?.newPlan : undefined;
  const [taken, setTaken] = useState<NewPlanState['newPlan'] | null>(null);
  if (offered && offered !== taken) setTaken(offered);
  else if (!offered && taken) {
    setTaken(null);
    setDialog({ open: true, sourceId: taken.sourceId, vmIds: taken.vmIds, sourceProject: taken.sourceProject });
  }
  useEffect(() => {
    if (offered) navigate(location.pathname, { replace: true, state: null });
  }, [offered, location.pathname, navigate]);

  const providerName = useMemo(() => new Map((providers.data ?? []).map((p) => [p.id, providerLabel(p)])), [providers.data]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = (plans.data ?? []).filter(
      (p) => (status === 'all' || p.derived_status === status) && (!q || `${p.name} ${p.id} ${p.description ?? ''}`.toLowerCase().includes(q)),
    );
    return sortBy(filtered, sort, (p, key) => {
      switch (key) {
        case 'name':
          return p.name;
        case 'status':
          return DERIVED_PLAN_STATUSES.indexOf(p.derived_status);
        case 'vms':
          return p.vm_ids.length;
        case 'done': {
          const pr = planProgress(p);
          return pr?.total ? pr.done / pr.total : 0;
        }
        case 'slo':
          return p.downtime_slo_s;
        case 'updated':
          return Date.parse(p.updated_at);
      }
    });
  }, [plans.data, query, status, sort]);

  const onSort = (key: SortKey) => {
    const next = nextSort(sort, key);
    changeParams({ sort: next.key, direction: next.direction });
  };

  return (
    <>
      <PageHeader
        title="Plans"
        description="Each plan moves a set of VMs from one source to one destination with its own mappings, waves, downtime SLO and approval rules."
        actions={
          <Button
            size="lg"
            variant="primary"
            icon={Plus}
            disabledReason={canCreate ? null : 'Creating plans requires the operator role.'}
            onClick={() => setDialog({ open: true })}
          >
            New plan
          </Button>
        }
      />

      <div className="mb-3 grid grid-cols-2 gap-2 md:flex md:items-end">
        <div className="col-span-2 min-w-0 md:w-72">
          <label htmlFor={searchId} className="field-label">
            Search plans
          </label>
          <input ref={searchRef} id={searchId} type="search" className="input" placeholder="Name, id or description" value={query} onChange={(e) => changeParams({ q: e.target.value })} />
        </div>
        <SelectField
          label="Status"
          className="md:w-44"
          value={status}
          onChange={(e) => changeParams({ status: e.target.value === 'all' ? '' : e.target.value })}
          options={[{ value: 'all', label: 'All' }, ...DERIVED_PLAN_STATUSES.map((s) => ({ value: s, label: planStatusMeta(s).label }))]}
        />
        {(query || status !== 'all') && (
          <Button onClick={() => { changeParams({ q: '', status: '', sort: '', direction: '' }); searchRef.current?.focus(); }}>Clear filters</Button>
        )}
      </div>

      {/* a live region already on the page: what the filters show is read out, none too (SDD §16) */}
      <p role="status" className="mb-2 text-xs text-muted-foreground empty:hidden">
        {plans.data && plans.data.length > 0 ? `Showing ${formatNumber(rows.length)} of ${formatNumber(plans.data.length)} plans` : ''}
      </p>
      {plans.isPending && <LoadingBlock label="Loading plans…" rows={5} />}
      {plans.error && <ErrorBanner error={plans.error} title="Plans are unavailable" onRetry={() => void plans.refetch()} />}
      {/* without the providers each plan shows its provider ids, not their names (SDD §16) */}
      {providers.error && (
        <ErrorBanner error={providers.error} title="Providers are unavailable: the plans show provider ids" onRetry={() => void providers.refetch()} />
      )}
      {plans.data && plans.data.length === 0 && (
        <EmptyState
          icon={ClipboardList}
          title="No plans yet"
          description="Create a plan: pick a source, the VMs and the destination, then validate it."
          action={
            canCreate ? (
              <Button size="lg" variant="primary" icon={Plus} onClick={() => setDialog({ open: true })}>
                New plan
              </Button>
            ) : undefined
          }
        />
      )}
      {plans.data && plans.data.length > 0 && rows.length === 0 && <EmptyState icon={SearchX} title="No plans match these filters" />}
      {rows.length > 0 && (
        <div className="table-wrap rounded-lg border border-border">
          <table className="data-table">
            <caption className="sr-only">Migration plans</caption>
            <thead>
              <tr>
                <SortableHeader label="Plan" sortKey="name" sort={sort} onSort={onSort} />
                <SortableHeader label="Status" sortKey="status" sort={sort} onSort={onSort} className="hidden lg:table-cell" />
                <th scope="col" className="hidden md:table-cell">
                  Source → destination
                </th>
                <SortableHeader label="VMs" sortKey="vms" sort={sort} onSort={onSort} align="right" />
                <SortableHeader label="Done" sortKey="done" sort={sort} onSort={onSort} className="hidden sm:table-cell" />
                <SortableHeader label="SLO" sortKey="slo" sort={sort} onSort={onSort} align="right" className="hidden lg:table-cell" />
                <SortableHeader label="Updated" sortKey="updated" sort={sort} onSort={onSort} className="hidden lg:table-cell" />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const pr = planProgress(p);
                return (
                  <tr key={p.id}>
                    <th scope="row" className="sm:min-w-48">
                      <Link to={`/plans/${p.id}`} className="font-medium underline-offset-4 hover:underline">
                        {p.name}
                      </Link>
                      <span className="block font-mono text-xs font-normal text-muted-foreground">{p.id}</span>
                      <span className="mt-1.5 inline-flex max-w-full lg:hidden">
                        <PlanStatusBadge status={p.derived_status} nothingMigrated={nothingMigrated(p)} />
                      </span>
                      {/* on a phone the Done and clouds columns are hidden: their facts stand under the name (SDD §16) */}
                      <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-normal sm:hidden">
                        {pr === null ? (
                          <NothingLeft />
                        ) : (
                          <>
                            {/* never a dot on a 320 px screen: the count wraps under the bar instead */}
                            <ProgressBar
                              value={pr.total ? (pr.done / pr.total) * 100 : 0}
                              label={`${p.name}: migrations done`}
                              tone="success"
                              size="sm"
                              className="min-w-16 flex-1"
                            />
                            <span className="num shrink-0 text-xs">
                              {pr.done}/{pr.total} done
                            </span>
                          </>
                        )}
                      </span>
                      {pr?.cancelled ? <CancelledLine n={pr.cancelled} className="sm:hidden" /> : null}
                      <span className="mt-0.5 block text-xs font-normal text-muted-foreground sm:hidden">
                        {providerName.get(p.source_provider_id) ?? p.source_provider_id} → {providerName.get(p.destination_provider_id) ?? p.destination_provider_id}
                      </span>
                    </th>
                    <td className="hidden min-w-32 lg:table-cell">
                      <PlanStatusBadge status={p.derived_status} nothingMigrated={nothingMigrated(p)} />
                    </td>
                    <td className="hidden text-sm md:table-cell">
                      {providerName.get(p.source_provider_id) ?? p.source_provider_id}
                      <span className="text-muted-foreground"> → </span>
                      {providerName.get(p.destination_provider_id) ?? p.destination_provider_id}
                    </td>
                    <td className="num text-right">{p.vm_ids.length}</td>
                    <td className="hidden w-40 sm:table-cell">
                      {pr === null ? (
                        <NothingLeft />
                      ) : (
                        <>
                          <div className="flex items-center gap-2">
                            <ProgressBar value={pr.total ? (pr.done / pr.total) * 100 : 0} label={`${p.name}: migrations done`} tone="success" size="sm" />
                            <span className="num shrink-0 text-xs">
                              {pr.done}/{pr.total}
                            </span>
                          </div>
                          {pr.cancelled ? <CancelledLine n={pr.cancelled} /> : null}
                        </>
                      )}
                    </td>
                    <td className="num hidden whitespace-nowrap text-right lg:table-cell">{formatDuration(p.downtime_slo_s)}</td>
                    <td className="hidden whitespace-nowrap text-muted-foreground lg:table-cell">
                      <time dateTime={p.updated_at} title={formatDateTime(p.updated_at)}>
                        {formatRelative(p.updated_at)}
                      </time>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <PlanCreateDialog
        open={dialog.open}
        onClose={() => setDialog({ open: false })}
        initialSourceId={dialog.sourceId}
        initialVmIds={dialog.vmIds}
        initialSourceProject={dialog.sourceProject}
      />
    </>
  );
}
