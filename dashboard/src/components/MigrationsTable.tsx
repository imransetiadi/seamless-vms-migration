import { SearchX } from 'lucide-react';
import { useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { SEVERITIES, type Migration, type Phase, type Plan, type Strategy } from '../api/types';
import { formatDuration, formatNumber, formatPct } from '../lib/format';
import { isActivePhase, PHASE_ORDER, phaseMeta, phaseSortKey } from '../lib/phase';
import { nextSort, sortBy, type SortState } from '../lib/sort';
import { STRATEGY_LABELS, strategyLabel } from '../lib/status';
import { Button } from './Button';
import { EmptyState } from './EmptyState';
import { EstimateCell } from './EstimateCell';
import { SelectField } from './Field';
import { FindingsSummary } from './FindingsList';
import { ProgressBar } from './ProgressBar';
import { SortableHeader } from './SortableHeader';
import { PhaseBadge } from './StatusBadge';

type SortKey = 'vm' | 'phase' | 'strategy' | 'wave' | 'progress' | 'estimate' | 'findings' | 'downtime';

export interface MigrationsTableProps {
  migrations: Migration[];
  /** Supplies wave names and order. */
  plan?: Plan | null;
  caption?: string;
  /** What to say, and the action to offer, when the plan has no migrations at all (SDD §16). */
  emptyDescription?: ReactNode;
  emptyAction?: ReactNode;
}

function findingScore(m: Migration): number {
  // blockers dominate, then warnings, then info.
  return SEVERITIES.reduce((score, severity, i) => score + m.findings.filter((f) => f.severity === severity).length * 1000 ** (2 - i), 0);
}

/** Plan migrations with strategy, estimate and findings; text/phase/strategy/wave filters; sortable. */
export function MigrationsTable({ migrations, plan, caption = 'Migrations', emptyDescription, emptyAction }: MigrationsTableProps) {
  const searchId = useId();
  const [query, setQuery] = useState('');
  const [phase, setPhase] = useState<Phase | 'all'>('all');
  const [strategy, setStrategy] = useState<Strategy | 'all'>('all');
  const [wave, setWave] = useState('all');
  const [sort, setSort] = useState<SortState<SortKey> | null>(null);

  const waves = useMemo(() => [...(plan?.waves ?? [])].sort((a, b) => a.order - b.order), [plan]);
  const waveName = useMemo(() => new Map(waves.map((w) => [w.id, w.name])), [waves]);
  const waveOrder = useMemo(() => new Map(waves.map((w) => [w.id, w.order])), [waves]);
  const phasesPresent = useMemo(() => PHASE_ORDER.filter((p) => migrations.some((m) => m.phase === p)), [migrations]);
  const strategiesPresent = useMemo(
    () => (Object.keys(STRATEGY_LABELS) as Strategy[]).filter((s) => migrations.some((m) => m.strategy === s)),
    [migrations],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return migrations.filter((m) => {
      if (phase !== 'all' && m.phase !== phase) return false;
      if (strategy !== 'all' && m.strategy !== strategy) return false;
      if (wave !== 'all' && m.wave_id !== wave) return false;
      if (!q) return true;
      return [m.vm.name, m.id, m.vm.project ?? ''].some((v) => v.toLowerCase().includes(q));
    });
  }, [migrations, query, phase, strategy, wave]);

  const sorted = useMemo(
    () =>
      sortBy(filtered, sort, (m, key) => {
        switch (key) {
          case 'vm':
            return m.vm.name;
          case 'phase':
            return phaseSortKey(m.phase);
          case 'strategy':
            return STRATEGY_LABELS[m.strategy] ?? m.strategy;
          case 'wave':
            return m.wave_id ? (waveOrder.get(m.wave_id) ?? null) : null;
          case 'progress':
            return m.progress_pct;
          case 'estimate':
            return m.estimate ? m.estimate.downtime_s : null;
          case 'findings':
            return findingScore(m);
          case 'downtime':
            return m.actual_downtime_s;
        }
      }),
    [filtered, sort, waveOrder],
  );

  const filtersActive = query !== '' || phase !== 'all' || strategy !== 'all' || wave !== 'all';
  const searchRef = useRef<HTMLInputElement>(null);
  const clear = () => {
    setQuery('');
    setPhase('all');
    setStrategy('all');
    setWave('all');
    // the button goes away with the filters: the search field keeps the focus (SDD §16)
    searchRef.current?.focus();
  };
  const onSort = (key: SortKey) => setSort(nextSort(sort, key));

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-end">
        <div className="col-span-2 min-w-0 md:w-60">
          <label htmlFor={searchId} className="field-label">
            Search migrations
          </label>
          <input
            ref={searchRef}
            id={searchId}
            type="search"
            className="input"
            placeholder="VM name, project or id"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <SelectField
          label="Phase"
          className="md:w-44"
          value={phase}
          onChange={(e) => setPhase(e.target.value as Phase | 'all')}
          options={[{ value: 'all', label: 'All' }, ...phasesPresent.map((p) => ({ value: p, label: phaseMeta(p).label }))]}
        />
        <SelectField
          label="Strategy"
          className="md:w-44"
          value={strategy}
          onChange={(e) => setStrategy(e.target.value as Strategy | 'all')}
          options={[{ value: 'all', label: 'All' }, ...strategiesPresent.map((s) => ({ value: s, label: STRATEGY_LABELS[s] }))]}
        />
        {waves.length > 0 && (
          <SelectField
            label="Wave"
            className="md:w-52"
            value={wave}
            onChange={(e) => setWave(e.target.value)}
            options={[{ value: 'all', label: 'All' }, ...waves.map((w) => ({ value: w.id, label: `${w.order}. ${w.name}` }))]}
          />
        )}
        {filtersActive && sorted.length > 0 && (
          <Button variant="ghost" onClick={clear}>
            Clear filters
          </Button>
        )}
      </div>

      <p role="status" className="text-xs text-muted-foreground">
        Showing {formatNumber(sorted.length)} of {formatNumber(migrations.length)} migrations
      </p>

      {sorted.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title={migrations.length === 0 ? 'No migrations in this plan yet' : 'No migrations match these filters'}
          description={migrations.length === 0 ? emptyDescription : undefined}
          action={migrations.length > 0 ? <Button onClick={clear}>Clear filters</Button> : emptyAction}
        />
      ) : (
        <div className="table-wrap rounded-lg border border-border">
          <table className="data-table">
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                <SortableHeader label="VM" sortKey="vm" sort={sort} onSort={onSort} />
                <SortableHeader label="Phase" sortKey="phase" sort={sort} onSort={onSort} />
                <SortableHeader label="Strategy" sortKey="strategy" sort={sort} onSort={onSort} />
                {waves.length > 0 && <SortableHeader label="Wave" sortKey="wave" sort={sort} onSort={onSort} className="hidden md:table-cell" />}
                <SortableHeader label="Progress" sortKey="progress" sort={sort} onSort={onSort} className="hidden lg:table-cell" />
                <SortableHeader label="Est. downtime" sortKey="estimate" sort={sort} onSort={onSort} />
                <SortableHeader label="Findings" sortKey="findings" sort={sort} onSort={onSort} />
                <SortableHeader label="Downtime" sortKey="downtime" sort={sort} onSort={onSort} align="right" className="hidden md:table-cell" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((m) => {
                const override = plan?.strategy_overrides[m.vm.source_id];
                return (
                  <tr key={m.id}>
                    <th scope="row" className="whitespace-nowrap font-mono">
                      <Link to={`/migrations/${m.id}`} className="underline-offset-4 hover:underline">
                        {m.vm.name}
                      </Link>
                    </th>
                    <td>
                      <PhaseBadge phase={m.phase} />
                    </td>
                    <td className="whitespace-nowrap">
                      {strategyLabel(m.strategy)}
                      {override && <span className="block text-xs text-muted-foreground">override</span>}
                    </td>
                    {waves.length > 0 && (
                      <td className="hidden max-w-48 truncate md:table-cell" title={m.wave_id ? waveName.get(m.wave_id) : undefined}>
                        {m.wave_id ? (waveName.get(m.wave_id) ?? m.wave_id) : '—'}
                      </td>
                    )}
                    <td className="hidden w-36 lg:table-cell">
                      {isActivePhase(m.phase) || m.phase === 'awaiting_cutover' ? (
                        <div className="flex items-center gap-2">
                          <ProgressBar value={m.progress_pct} label={`${m.vm.name} progress`} size="sm" />
                          <span className="num w-12 text-right text-xs">{formatPct(m.progress_pct, 0)}</span>
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </td>
                    <td>
                      <EstimateCell estimate={m.estimate} compact />
                    </td>
                    <td>
                      <FindingsSummary findings={m.findings} />
                    </td>
                    <td className="num hidden whitespace-nowrap text-right md:table-cell">
                      {m.actual_downtime_s !== null
                        ? formatDuration(m.actual_downtime_s)
                        : m.downtime_started_at && !m.downtime_ended_at
                          ? <span className="text-status-warning">running</span>
                          : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
