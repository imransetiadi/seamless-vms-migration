import { ClipboardList, Plus, SearchX } from 'lucide-react';
import { useEffect, useId, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useMigrations, usePlans, useProviders } from '../api/hooks';
import { useRole } from '../api/session';
import { PLAN_STATUSES, type PlanStatus } from '../api/types';
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
import { formatDateTime, formatDuration, formatRelative } from '../lib/format';
import { hasRole } from '../lib/roles';
import { nextSort, sortBy, type SortState } from '../lib/sort';
import { planStatusMeta } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

type SortKey = 'name' | 'status' | 'vms' | 'done' | 'slo' | 'updated';

/** Router state used by Inventory to open the dialog with a selection. */
export interface NewPlanState {
  newPlan?: { sourceId: string; vmIds: string[] };
}

export default function Plans() {
  usePageTitle('Plans');
  const searchId = useId();
  const plans = usePlans();
  const migrations = useMigrations();
  const providers = useProviders();
  const role = useRole();
  const location = useLocation();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<PlanStatus | 'all'>('all');
  const [sort, setSort] = useState<SortState<SortKey> | null>({ key: 'updated', direction: 'descending' });
  const [dialog, setDialog] = useState<{ open: boolean; sourceId?: string; vmIds?: string[] }>({ open: false });
  const canCreate = hasRole(role, 'operator');

  // Opened from Inventory with a VM selection.
  useEffect(() => {
    const state = location.state as NewPlanState | null;
    if (state?.newPlan && canCreate) {
      setDialog({ open: true, sourceId: state.newPlan.sourceId, vmIds: state.newPlan.vmIds });
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [location.state, location.pathname, navigate, canCreate]);

  const providerName = useMemo(() => new Map((providers.data ?? []).map((p) => [p.id, p.name])), [providers.data]);
  const progress = useMemo(() => {
    const map = new Map<string, { total: number; done: number }>();
    for (const m of migrations.data ?? []) {
      const entry = map.get(m.plan_id) ?? { total: 0, done: 0 };
      entry.total += 1;
      if (m.phase === 'completed' || m.phase === 'finalized') entry.done += 1;
      map.set(m.plan_id, entry);
    }
    return map;
  }, [migrations.data]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = (plans.data ?? []).filter(
      (p) => (status === 'all' || p.status === status) && (!q || `${p.name} ${p.id} ${p.description ?? ''}`.toLowerCase().includes(q)),
    );
    return sortBy(filtered, sort, (p, key) => {
      switch (key) {
        case 'name':
          return p.name;
        case 'status':
          return PLAN_STATUSES.indexOf(p.status);
        case 'vms':
          return p.vm_ids.length;
        case 'done': {
          const pr = progress.get(p.id);
          return pr && pr.total ? pr.done / pr.total : 0;
        }
        case 'slo':
          return p.downtime_slo_s;
        case 'updated':
          return Date.parse(p.updated_at);
      }
    });
  }, [plans.data, query, status, sort, progress]);

  const onSort = (key: SortKey) => setSort(nextSort(sort, key));

  return (
    <>
      <PageHeader
        title="Plans"
        description="Each plan moves a set of VMs from one source to RHOSO with its own mappings, waves, downtime SLO and approval rules."
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
          <input id={searchId} type="search" className="input" placeholder="Name, id or description" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <SelectField
          label="Status"
          className="md:w-44"
          value={status}
          onChange={(e) => setStatus(e.target.value as PlanStatus | 'all')}
          options={[{ value: 'all', label: 'All' }, ...PLAN_STATUSES.map((s) => ({ value: s, label: planStatusMeta(s).label }))]}
        />
      </div>

      {plans.isPending && <LoadingBlock label="Loading plans…" rows={5} />}
      {plans.error && <ErrorBanner error={plans.error} title="Plans are unavailable" onRetry={() => void plans.refetch()} />}
      {plans.data && plans.data.length === 0 && (
        <EmptyState
          icon={ClipboardList}
          title="No plans yet"
          description="Create a plan: pick a source, the VMs and the RHOSO destination, then validate it."
          action={
            canCreate ? (
              <Button variant="primary" icon={Plus} onClick={() => setDialog({ open: true })}>
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
                <SortableHeader label="Status" sortKey="status" sort={sort} onSort={onSort} />
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
                const pr = progress.get(p.id) ?? { total: p.vm_ids.length, done: 0 };
                return (
                  <tr key={p.id}>
                    <th scope="row" className="min-w-48">
                      <Link to={`/plans/${p.id}`} className="font-medium underline-offset-4 hover:underline">
                        {p.name}
                      </Link>
                      <span className="block font-mono text-xs font-normal text-muted-foreground">{p.id}</span>
                    </th>
                    <td>
                      <PlanStatusBadge status={p.status} />
                    </td>
                    <td className="hidden text-sm md:table-cell">
                      {providerName.get(p.source_provider_id) ?? p.source_provider_id}
                      <span className="text-muted-foreground"> → </span>
                      {providerName.get(p.destination_provider_id) ?? p.destination_provider_id}
                    </td>
                    <td className="num text-right">{p.vm_ids.length}</td>
                    <td className="hidden w-40 sm:table-cell">
                      <div className="flex items-center gap-2">
                        <ProgressBar value={pr.total ? (pr.done / pr.total) * 100 : 0} label={`${p.name}: migrations done`} tone="success" size="sm" />
                        <span className="num shrink-0 text-xs">
                          {pr.done}/{pr.total}
                        </span>
                      </div>
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
      />
    </>
  );
}
