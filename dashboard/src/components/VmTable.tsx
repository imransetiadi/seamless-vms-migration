import {
  CircleCheck,
  CircleHelp,
  CircleX,
  Pause,
  Power,
  PowerOff,
  SearchX,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { POWER_STATES, type PowerState, type ProviderKind, type VMRef } from '../api/types';
import { cn } from '../lib/cn';
import { formatBytes, formatNumber } from '../lib/format';
import type { Tone } from '../lib/phase';
import { readinessFlags, readinessLevel, type ReadinessFlag, type ReadinessLevel } from '../lib/readiness';
import { sortBy, nextSort, type SortState } from '../lib/sort';
import { severityMeta, TONE_CLASSES } from '../lib/status';
import { Button } from './Button';
import { EmptyState } from './EmptyState';
import { SelectField } from './Field';
import { SortableHeader } from './SortableHeader';
import { StatusBadge } from './StatusBadge';

const POWER_META: Record<PowerState, { label: string; tone: Tone; icon: LucideIcon }> = {
  running: { label: 'Running', tone: 'success', icon: Power },
  stopped: { label: 'Stopped', tone: 'neutral', icon: PowerOff },
  paused: { label: 'Paused', tone: 'warning', icon: Pause },
  error: { label: 'Error', tone: 'danger', icon: CircleX },
  unknown: { label: 'Unknown', tone: 'neutral', icon: CircleHelp },
};

const READINESS_ORDER: Record<ReadinessLevel, number> = { blocker: 0, attention: 1, ready: 2 };

type SortKey = 'name' | 'project' | 'power' | 'cpu' | 'disk' | 'used' | 'readiness';
type ReadinessFilter = 'all' | ReadinessLevel;

interface Row {
  vm: VMRef;
  flags: ReadinessFlag[];
  level: ReadinessLevel;
  haystack: string;
}

export interface VmTableProps {
  vms: VMRef[];
  providerKind: ProviderKind;
  /** Enables the selection column (plan creation). */
  selected?: ReadonlySet<string>;
  onSelectedChange?: (next: Set<string>) => void;
  caption?: string;
  pageSize?: number;
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
        return <StatusBadge key={flag.code} tone={meta.tone} icon={meta.icon} label={flag.label} title={`${flag.code}: ${flag.detail}`} />;
      })}
    </div>
  );
}

/** Inventory table with search, power/readiness/project filters, sorting and optional selection. */
export function VmTable({ vms, providerKind, selected, onSelectedChange, caption = 'Virtual machines', pageSize = 200 }: VmTableProps) {
  const searchId = useId();
  const [query, setQuery] = useState('');
  const [power, setPower] = useState<PowerState | 'all'>('all');
  const [readiness, setReadiness] = useState<ReadinessFilter>('all');
  const [project, setProject] = useState('all');
  const [sort, setSort] = useState<SortState<SortKey> | null>(null);
  const [limit, setLimit] = useState(pageSize);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const selectable = Boolean(selected && onSelectedChange);

  const rows = useMemo<Row[]>(
    () =>
      vms.map((vm) => {
        const flags = readinessFlags(vm, providerKind);
        const haystack = [vm.name, vm.project, vm.os_type, vm.flavor, vm.host, ...Object.entries(vm.tags).flat()]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return { vm, flags, level: readinessLevel(flags), haystack };
      }),
    [vms, providerKind],
  );

  const projects = useMemo(
    () => [...new Set(vms.map((vm) => vm.project).filter((p): p is string => Boolean(p)))].sort(),
    [vms],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter(({ vm, level, haystack }) => {
      if (power !== 'all' && vm.power_state !== power) return false;
      if (project !== 'all' && vm.project !== project) return false;
      if (readiness === 'ready' && level !== 'ready') return false;
      if (readiness === 'attention' && level === 'ready') return false;
      if (readiness === 'blocker' && level !== 'blocker') return false;
      return !q || haystack.includes(q);
    });
  }, [rows, query, power, project, readiness]);

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
  const filtersActive = query !== '' || power !== 'all' || readiness !== 'all' || project !== 'all';
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
  };

  const toggle = (id: string) => {
    if (!selected || !onSelectedChange) return;
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectedChange(next);
  };

  const toggleAllShown = () => {
    if (!selected || !onSelectedChange) return;
    const next = new Set(selected);
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
        {projects.length > 1 && (
          <SelectField
            label="Project"
            className="md:w-40"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            options={[{ value: 'all', label: 'All' }, ...projects.map((p) => ({ value: p, label: p }))]}
          />
        )}
        {filtersActive && filtered.length > 0 && (
          <Button variant="ghost" onClick={clearFilters} className="md:mb-0.5">
            Clear filters
          </Button>
        )}
      </div>

      <p role="status" className="text-xs text-muted-foreground">
        Showing {formatNumber(filtered.length)} of {formatNumber(vms.length)} VMs
        {selectable && `, ${formatNumber(selected?.size ?? 0)} selected`}
      </p>

      {filtered.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title="No VMs match these filters"
          description="Try a different search term or clear the filters."
          action={<Button onClick={clearFilters}>Clear filters</Button>}
        />
      ) : (
        <div className="table-wrap rounded-lg border border-border">
          <table className="data-table">
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                {selectable && (
                  <th scope="col" className="w-10">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      className="size-4 cursor-pointer accent-accent"
                      aria-label={`Select all ${filtered.length} shown`}
                      checked={allShownSelected}
                      onChange={toggleAllShown}
                    />
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
              {visible.map(({ vm, flags, level }) => {
                const isSelected = selected?.has(vm.source_id) ?? false;
                return (
                  <tr key={vm.source_id} className={cn(isSelected && 'bg-accent/5')}>
                    {selectable && (
                      <td>
                        <input
                          type="checkbox"
                          className="size-4 cursor-pointer accent-accent"
                          aria-label={`Select ${vm.name}`}
                          checked={isSelected}
                          onChange={() => toggle(vm.source_id)}
                        />
                      </td>
                    )}
                    <th scope="row" className="whitespace-nowrap font-mono">
                      {vm.name}
                    </th>
                    <td>
                      <ReadinessCell flags={flags} level={level} />
                    </td>
                    <td className="hidden text-muted-foreground md:table-cell">{vm.project ?? '—'}</td>
                    <td>
                      <PowerCell state={vm.power_state} />
                    </td>
                    <td className="num whitespace-nowrap text-right">
                      {vm.vcpus} / {formatBytes(vm.ram_mb * 1024 * 1024)}
                    </td>
                    <td className="num whitespace-nowrap text-right">
                      <span className="text-muted-foreground">{vm.disks.length}× </span>
                      {formatBytes(vm.disk_bytes)}
                    </td>
                    <td className="num hidden whitespace-nowrap text-right md:table-cell">{formatBytes(vm.used_bytes)}</td>
                    <td className="hidden text-muted-foreground lg:table-cell">{vm.os_type ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {sorted.length > visible.length && (
        <div>
          <Button onClick={() => setLimit((n) => n + pageSize)}>Show {Math.min(pageSize, sorted.length - visible.length)} more</Button>
        </div>
      )}
    </div>
  );
}
