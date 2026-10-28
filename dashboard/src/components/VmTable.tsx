import { CircleCheck, CircleHelp, CircleX, Pause, Power, PowerOff, RefreshCw, SearchX, type LucideIcon } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { POWER_STATES, type GpuDevice, type GuestOS, type PowerState, type ProviderKind, type VMRef } from '../api/types';
import { cn } from '../lib/cn';
import { formatBytes, formatNumber } from '../lib/format';
import { gpuLabel, vmGpus } from '../lib/gpu';
import { phaseMeta, type Tone } from '../lib/phase';
import type { Holder } from '../lib/holds';
import { readinessFlags, readinessLevel, type ReadinessFlag, type ReadinessLevel } from '../lib/readiness';
import { sortBy, nextSort, type SortState } from '../lib/sort';
import { severityMeta, TONE_CLASSES } from '../lib/status';
import { Button } from './Button';
import { Checkbox } from './Checkbox';
import { EmptyState } from './EmptyState';
import { SelectField } from './Field';
import { SortableHeader } from './SortableHeader';
import { StatusBadge } from './StatusBadge';
import { TechPreviewBadge } from './TechPreviewBadge';
import { guestOsOf } from '../lib/guestOs';

const POWER_META: Record<PowerState, { label: string; tone: Tone; icon: LucideIcon }> = {
  running: { label: 'Running', tone: 'success', icon: Power },
  stopped: { label: 'Stopped', tone: 'neutral', icon: PowerOff },
  paused: { label: 'Paused', tone: 'warning', icon: Pause },
  error: { label: 'Error', tone: 'danger', icon: CircleX },
  transitioning: { label: 'Task in flight', tone: 'warning', icon: RefreshCw },
  unknown: { label: 'Unknown', tone: 'neutral', icon: CircleHelp },
};

const READINESS_ORDER: Record<ReadinessLevel, number> = { blocker: 0, attention: 1, ready: 2 };

type SortKey = 'name' | 'project' | 'power' | 'cpu' | 'disk' | 'used' | 'readiness';
type ReadinessFilter = 'all' | ReadinessLevel;

type OsFilter = 'all' | 'linux' | 'windows' | 'legacy' | 'unknown';
type PlanFilter = 'all' | 'free' | 'held';
type GpuFilter = 'all' | 'with' | 'without';

interface Row {
  vm: VMRef;
  flags: ReadinessFlag[];
  level: ReadinessLevel;
  haystack: string;
  guest: GuestOS;
  /** Its GPUs as the API reads them (SDD §22.2): as its source reports them, else its flavor's requests. */
  gpus: GpuDevice[];
}

export interface VmTableProps {
  vms: VMRef[];
  providerKind: ProviderKind;
  /** Enables the selection column (plan creation). */
  selected?: ReadonlySet<string>;
  onSelectedChange?: (next: Set<string>) => void;
  caption?: string;
  pageSize?: number;
  /** The search the table opens with: a link to one VM (SDD §16, ADV-02). */
  initialQuery?: string;
  /**
   * The plans whose migrations hold each VM, by source id (SDD §5.4, §16): named in the readiness cell, with a Plan
   * filter; null while they cannot be known (the table says so), undefined where the caller does not track them.
   */
  holders?: ReadonlyMap<string, readonly Holder[]> | null;
}

function PowerCell({ state }: { state: PowerState }) {
  const meta = POWER_META[state] ?? POWER_META.unknown;
  return (
    <span className={cn('inline-flex items-center gap-1.5 whitespace-nowrap', TONE_CLASSES[meta.tone].text)}>
      <meta.icon aria-hidden className="size-3.5 shrink-0" />
      {meta.label}
    </span>
  );
}

function ReadinessCell({ flags, level }: { flags: ReadinessFlag[]; level: ReadinessLevel }) {
  const shown = level === 'ready' ? flags : flags.filter((f) => f.severity !== 'info').concat(flags.filter((f) => f.severity === 'info'));
  return (
    <div className="flex flex-wrap gap-1">
      {level === 'ready' && <StatusBadge tone="success" icon={CircleCheck} label="Ready" />}
      {shown.map((flag) => {
        const meta = severityMeta(flag.severity);
        return <StatusBadge key={flag.code} tone={meta.tone} icon={meta.icon} label={flag.label} detail={`${flag.code}: ${flag.detail}`} />;
      })}
    </div>
  );
}

/** Inventory table with search, power/readiness/project filters, sorting and optional selection. */
export function VmTable({ vms, providerKind, selected, onSelectedChange, caption = 'Virtual machines', pageSize = 200, initialQuery = '', holders }: VmTableProps) {
  const searchId = useId();
  const [query, setQuery] = useState(initialQuery);
  const [power, setPower] = useState<PowerState | 'all'>('all');
  const [readiness, setReadiness] = useState<ReadinessFilter>('all');
  const [project, setProject] = useState('all');
  const [os, setOs] = useState<OsFilter>('all');
  const [plan, setPlan] = useState<PlanFilter>('all');
  const [gpu, setGpu] = useState<GpuFilter>('all');
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [sort, setSort] = useState<SortState<SortKey> | null>(null);
  const [limit, setLimit] = useState(pageSize);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const selectable = Boolean(selected && onSelectedChange);

  const rows = useMemo<Row[]>(
    () =>
      vms.map((vm) => {
        const flags = readinessFlags(vm, providerKind);
        const guest = guestOsOf(vm);
        const haystack = [vm.name, vm.project, vm.os_type, guest.label, vm.flavor, vm.host, ...Object.entries(vm.tags).flat()]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return { vm, flags, level: readinessLevel(flags), haystack, guest, gpus: vmGpus(vm) };
      }),
    [vms, providerKind],
  );
  // the GPU column and filter while a VM of the table has a GPU, as the Project filter while it has two projects (SDD §16)
  const hasGpus = rows.some((r) => r.gpus.length > 0);
  const gpuFilter = hasGpus ? gpu : 'all';

  const projects = useMemo(
    () => [...new Set(vms.map((vm) => vm.project).filter((p): p is string => Boolean(p)))].sort(),
    [vms],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter(({ vm, level, haystack, guest, gpus }) => {
      if (selectedOnly && !selected?.has(vm.source_id)) return false;
      if (power !== 'all' && vm.power_state !== power) return false;
      if (gpuFilter !== 'all' && (gpus.length > 0) !== (gpuFilter === 'with')) return false;
      if (os === 'legacy' ? guest.lifecycle !== 'legacy' : os !== 'all' && guest.family !== os) return false;
      if (project !== 'all' && vm.project !== project) return false;
      if (readiness === 'ready' && level !== 'ready') return false;
      if (readiness === 'attention' && level === 'ready') return false;
      if (readiness === 'blocker' && level !== 'blocker') return false;
      // the VMs no plan holds, or those one holds (SDD §16): unknown holders filter nothing
      if (holders && plan !== 'all' && holders.has(vm.source_id) !== (plan === 'held')) return false;
      return !q || haystack.includes(q);
    });
  }, [rows, query, power, project, readiness, os, plan, holders, gpuFilter, selectedOnly, selected]);

  const sorted = useMemo(
    () =>
      sortBy(filtered, sort, ({ vm, level }, key) => {
        switch (key) {
          case 'name':
            return vm.name;
          case 'project':
            return vm.project;
          case 'power':
            return vm.power_state;
          case 'cpu':
            return vm.vcpus * 1e6 + vm.ram_mb;
          case 'disk':
            return vm.disk_bytes;
          case 'used':
            return vm.used_bytes;
          case 'readiness':
            return READINESS_ORDER[level];
        }
      }),
    [filtered, sort],
  );

  const visible = sorted.slice(0, limit);
  const filtersActive =
    query !== '' || power !== 'all' || readiness !== 'all' || project !== 'all' || os !== 'all' || plan !== 'all' || gpuFilter !== 'all' || selectedOnly;
  // the header checkbox acts on every matching VM, also the rows not rendered yet: the labels say so
  const allMatchingShown = visible.length === filtered.length;
  const visibleIds = new Set(visible.map((r) => r.vm.source_id));
  const notShownSelected = selectable ? [...(selected ?? [])].filter((id) => !visibleIds.has(id)).length : 0;
  const showing = allMatchingShown
    ? `Showing ${formatNumber(filtered.length)} of ${formatNumber(vms.length)} VMs`
    : filtered.length === vms.length
      ? `Showing ${formatNumber(visible.length)} of ${formatNumber(vms.length)} VMs`
      : `Showing ${formatNumber(visible.length)} of ${formatNumber(filtered.length)} matching VMs (${formatNumber(vms.length)} in total)`;
  const shownSelected = selectable ? filtered.filter((r) => selected?.has(r.vm.source_id)).length : 0;
  const allShownSelected = filtered.length > 0 && shownSelected === filtered.length;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = shownSelected > 0 && !allShownSelected;
  }, [shownSelected, allShownSelected]);

  const clearFilters = () => {
    setQuery('');
    setPower('all');
    setReadiness('all');
    setProject('all');
    setOs('all');
    setPlan('all');
    setGpu('all');
    setSelectedOnly(false);
    // the button goes away with the filters: the search field keeps the focus (SDD §16)
    searchRef.current?.focus();
  };

  const toggle = (id: string) => {
    if (!selected || !onSelectedChange) return;
    const next = new Set(selected);
    if (selectedOnly && next.has(id)) searchRef.current?.focus();
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectedChange(next);
  };

  const toggleAllShown = () => {
    if (!selected || !onSelectedChange) return;
    const next = new Set(selected);
    if (selectedOnly && allShownSelected) searchRef.current?.focus();
    for (const { vm } of filtered) {
      if (allShownSelected) next.delete(vm.source_id);
      else next.add(vm.source_id);
    }
    onSelectedChange(next);
  };

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-end">
        <div className="col-span-2 min-w-0 md:w-64">
          <label htmlFor={searchId} className="field-label">
            Search VMs
          </label>
          <input
            ref={searchRef}
            id={searchId}
            type="search"
            className="input"
            placeholder="Name, project, OS or tag"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setLimit(pageSize);
            }}
          />
        </div>
        <SelectField
          label="Power state"
          className="md:w-40"
          value={power}
          onChange={(e) => setPower(e.target.value as PowerState | 'all')}
          options={[{ value: 'all', label: 'All' }, ...POWER_STATES.map((p) => ({ value: p, label: POWER_META[p].label }))]}
        />
        <SelectField
          label="Readiness"
          className="md:w-44"
          value={readiness}
          onChange={(e) => setReadiness(e.target.value as ReadinessFilter)}
          options={[
            { value: 'all', label: 'All' },
            { value: 'ready', label: 'Ready' },
            { value: 'attention', label: 'Needs attention' },
            { value: 'blocker', label: 'Blocked' },
          ]}
        />
        <SelectField
          label="Guest OS"
          className="md:w-40"
          value={os}
          onChange={(e) => setOs(e.target.value as OsFilter)}
          options={[
            { value: 'all', label: 'All' },
            { value: 'linux', label: 'Linux' },
            { value: 'windows', label: 'Windows' },
            { value: 'legacy', label: 'Legacy releases' },
            { value: 'unknown', label: 'Not identified' },
          ]}
        />
        {hasGpus && (
          <SelectField
            label="GPU"
            // its own badge, shown at every width: a narrow screen hides the GPU column and its badge (SDD §16, §22.11)
            aside={<TechPreviewBadge />}
            className="md:w-48"
            value={gpuFilter}
            onChange={(e) => setGpu(e.target.value as GpuFilter)}
            options={[
              { value: 'all', label: 'All' },
              { value: 'with', label: 'With a GPU' },
              { value: 'without', label: 'Without a GPU' },
            ]}
          />
        )}
        {projects.length > 1 && (
          <SelectField
            label="Project"
            className="md:w-40"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            options={[{ value: 'all', label: 'All' }, ...projects.map((p) => ({ value: p, label: p }))]}
          />
        )}
        {holders && (
          <SelectField
            label="Plan"
            className="md:w-40"
            value={plan}
            onChange={(e) => setPlan(e.target.value as PlanFilter)}
            options={[
              { value: 'all', label: 'All' },
              { value: 'free', label: 'Not in a plan' },
              { value: 'held', label: 'In a plan' },
            ]}
          />
        )}
        {filtersActive && filtered.length > 0 && (
          <Button variant="ghost" onClick={clearFilters} className="md:mb-0.5">
            Clear filters
          </Button>
        )}
      </div>

      {holders === null && (
        // never read as free while the plans that may hold them are unknown (SDD §16)
        <p className="text-xs text-status-warning">Which plans hold these VMs is unknown: the plans or their migrations could not be loaded.</p>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <p role="status" className="text-xs text-muted-foreground">
          {showing}
          {selectable && `, ${formatNumber(selected?.size ?? 0)} selected`}
          {notShownSelected > 0 && ` (${formatNumber(notShownSelected)} not shown)`}
        </p>
        {selectable && <Checkbox label="Selected only" checked={selectedOnly} onChange={setSelectedOnly} />}
        {selectable && (selected?.size ?? 0) > 0 && (
          <Button variant="ghost" onClick={() => {
            onSelectedChange?.(new Set());
            setSelectedOnly(false);
            searchRef.current?.focus();
          }}>
            Clear selection
          </Button>
        )}
      </div>

      {filtered.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title="No VMs match these filters"
          description="Try a different search term or clear the filters."
          action={<Button onClick={clearFilters}>Clear filters</Button>}
        />
      ) : (
        <div className="table-wrap rounded-lg border border-border" onFocus={(event) => event.target.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })}>
          <table ref={tableRef} tabIndex={-1} className="data-table outline-hidden">
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                {selectable && (
                  <th scope="col" className="w-14">
                    <label className="flex size-11 cursor-pointer items-center justify-center">
                      <input
                        ref={selectAllRef}
                        type="checkbox"
                        className="size-4 cursor-pointer accent-accent"
                        aria-label={
                          allMatchingShown ? `Select all ${filtered.length} shown` : `Select all ${filtered.length} matching VMs, ${visible.length} shown`
                        }
                        checked={allShownSelected}
                        onChange={toggleAllShown}
                      />
                    </label>
                  </th>
                )}
                <SortableHeader label="VM" sortKey="name" sort={sort} onSort={(k) => setSort(nextSort(sort, k))} />
                <SortableHeader label="Readiness" sortKey="readiness" sort={sort} onSort={(k) => setSort(nextSort(sort, k))} />
                <SortableHeader
                  label="Project"
                  sortKey="project"
                  sort={sort}
                  onSort={(k) => setSort(nextSort(sort, k))}
                  className="hidden md:table-cell"
                />
                <SortableHeader label="Power" sortKey="power" sort={sort} onSort={(k) => setSort(nextSort(sort, k))} />
                <SortableHeader label="vCPU / RAM" sortKey="cpu" sort={sort} onSort={(k) => setSort(nextSort(sort, k))} align="right" />
                {hasGpus && (
                  <th scope="col" className="hidden md:table-cell">
                    {/* a GPU element is labelled Technology Preview, in words (SDD §16, §22.11) */}
                    <span className="inline-flex items-center gap-1.5">
                      GPU
                      <TechPreviewBadge className="normal-case tracking-normal" />
                    </span>
                  </th>
                )}
                <SortableHeader label="Disks" sortKey="disk" sort={sort} onSort={(k) => setSort(nextSort(sort, k))} align="right" />
                <SortableHeader
                  label="Used"
                  sortKey="used"
                  sort={sort}
                  onSort={(k) => setSort(nextSort(sort, k))}
                  align="right"
                  className="hidden md:table-cell"
                />
                <th scope="col" className="hidden lg:table-cell">
                  OS
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map(({ vm, flags, level, gpus }) => {
                const isSelected = selected?.has(vm.source_id) ?? false;
                return (
                  <tr key={vm.source_id} className={cn(isSelected && 'bg-accent/5')}>
                    {selectable && (
                      <td>
                        <label className="flex size-11 cursor-pointer items-center justify-center">
                          <input
                            type="checkbox"
                            className="size-4 cursor-pointer accent-accent"
                            aria-label={`Select ${vm.name}`}
                            checked={isSelected}
                            onChange={() => toggle(vm.source_id)}
                          />
                        </label>
                      </td>
                    )}
                    <th scope="row" className="whitespace-nowrap font-mono">
                      {vm.name}
                    </th>
                    <td>
                      <ReadinessCell flags={flags} level={level} />
                      {holders?.get(vm.source_id)?.map((h) => (
                        // the plan whose migration holds it (SDD §5.4, §16): beside the name, never in the row header
                        <span key={h.planId} className="mt-0.5 block text-xs text-muted-foreground">
                          In plan{' '}
                          <Link to={`/plans/${encodeURIComponent(h.planId)}`} className="underline underline-offset-4">
                            {h.planName}
                          </Link>
                          , phase {phaseMeta(h.phase).label}
                        </span>
                      ))}
                    </td>
                    <td className="hidden text-muted-foreground md:table-cell">{vm.project ?? '—'}</td>
                    <td>
                      <PowerCell state={vm.power_state} />
                    </td>
                    <td className="num whitespace-nowrap text-right">
                      {vm.vcpus} / {formatBytes(vm.ram_mb * 1024 * 1024)}
                    </td>
                    {hasGpus && (
                      // each of its GPUs on a line of its own; empty for a VM without one (SDD §16)
                      <td className="hidden md:table-cell">
                        {gpus.map((g, i) => (
                          <span key={i} className="block">
                            {gpuLabel(g)}
                          </span>
                        ))}
                      </td>
                    )}
                    <td className="num whitespace-nowrap text-right">
                      <span className="text-muted-foreground">{vm.disks.length}× </span>
                      {formatBytes(vm.disk_bytes)}
                    </td>
                    <td className="num hidden whitespace-nowrap text-right md:table-cell">{formatBytes(vm.used_bytes)}</td>
                    <td className="hidden text-muted-foreground lg:table-cell" title={vm.os_type ?? undefined}>
                      {guestOsOf(vm).label}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {sorted.length > visible.length && (
        <div>
          <Button
            onClick={() => {
              // the last page removes this button: focus moves to the table, never to the page (SDD §16)
              if (sorted.length <= limit + pageSize) tableRef.current?.focus();
              setLimit((n) => n + pageSize);
            }}
          >
            Show {Math.min(pageSize, sorted.length - visible.length)} more
          </Button>
        </div>
      )}
    </div>
  );
}
