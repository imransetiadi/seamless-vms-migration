import { ArrowRight, Search, Server, X } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { MigrationSummary, Phase, Plan } from '../api/types';
import { useListState } from '../lib/useListState';
import { phaseMeta } from '../lib/phase';
import { Button } from './Button';
import { Modal } from './Modal';
import { PhaseBadge } from './StatusBadge';

const PAGE_SIZE = 8;

export function MigrationFinder({ migrations, plans, scope, initialPhase, returnFocusRef, onClose }: {
  migrations: readonly MigrationSummary[];
  plans: ReadonlyMap<string, Plan>;
  scope: string;
  initialPhase: Phase | 'all';
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const titleId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    // History traversal can reset focus after the restored dialog has already focused its input.
    const frame = requestAnimationFrame(() => {
      if (document.activeElement === document.body) inputRef.current?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, []);
  const visit = useListState('overview-finder');
  const [phase, setPhase] = visit.field('phase', initialPhase);
  const [query, setQuery] = visit.field('query', '');
  const [page, setPage] = visit.field('page', 1);
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return migrations.filter((m) => (phase === 'all' || m.phase === phase) && [
      m.vm.name, m.id, m.vm.source_id, m.vm.project, m.plan_id, plans.get(m.plan_id)?.name, phaseMeta(m.phase).label,
    ].some((field) => field?.toLowerCase().includes(q)))
      .sort((a, b) => a.vm.name.localeCompare(b.vm.name) || a.id.localeCompare(b.id));
  }, [migrations, phase, plans, query]);
  const last = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const current = Math.min(page, last);
  const start = (current - 1) * PAGE_SIZE;
  const shown = rows.slice(start, start + PAGE_SIZE);
  const count = `${rows.length} migration${rows.length === 1 ? '' : 's'}`;
  const resultCount = last > 1 ? `${start + 1}–${start + shown.length} of ${count}` : count;
  const emptyMessage = query.trim() || phase !== 'all' ? 'No matching migrations' : 'No migrations in this scope';
  return (
    <Modal open placement="top" onClose={onClose} labelledBy={titleId} initialFocusRef={inputRef} returnFocusRef={returnFocusRef} closeOnBackdrop>
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 id={titleId} className="text-lg font-semibold">Find migration</h2>
          <p className="wrap-anywhere text-sm text-muted-foreground">{scope}</p>
        </div>
        <Button size="lg" variant="ghost" aria-label="Close search" onClick={onClose}><X aria-hidden className="size-4" /></Button>
      </div>
      <div className="relative mx-4 my-3">
        <Search aria-hidden className="pointer-events-none absolute top-3 left-3 size-5 text-muted-foreground" />
        <input type="search" ref={inputRef} aria-label="Search migrations" placeholder="VM, plan, phase, project or ID"
          className="input pl-10" value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }} />
      </div>
      {phase !== 'all' && (
        <div className="mb-3 px-4">
          <Button size="sm" aria-label={`Remove ${phaseMeta(phase).label} phase filter`}
            onClick={() => { setPhase('all'); setPage(1); inputRef.current?.focus(); }}>
            {phaseMeta(phase).label}<X aria-hidden className="size-3.5" />
          </Button>
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pb-3">
        <p role="status" className="text-sm text-muted-foreground">
          {rows.length > 0 ? resultCount : emptyMessage}
        </p>
        {rows.length === 0 && query && (
          <Button onClick={() => { setQuery(''); setPage(1); inputRef.current?.focus(); }}>Clear search</Button>
        )}
        {last > 1 && (
          <div className="flex flex-wrap gap-1">
            <Button disabledReason={current === 1 ? 'First page' : null} onClick={() => setPage(current - 1)}>Previous</Button>
            <Button disabledReason={current === last ? 'Last page' : null} onClick={() => setPage(current + 1)}>Next</Button>
          </div>
        )}
      </div>
      {rows.length > 0 && (
        <ul className="border-t border-border">
          {shown.map((m) => (
            <li key={m.id} className="border-b border-border last:border-0">
              <Link to={`/migrations/${encodeURIComponent(m.id)}`}
                className="group flex min-h-11 items-center gap-3 px-4 py-2.5 hover:bg-muted/40 focus-visible:-outline-offset-2">
                <Server aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1 wrap-anywhere">
                  <p className="flex flex-wrap items-center gap-x-2 gap-y-1"><strong className="font-mono text-sm">{m.vm.name}</strong><PhaseBadge phase={m.phase} /></p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{plans.get(m.plan_id)?.name ?? m.plan_id} · {m.id}</p>
                </div>
                <ArrowRight aria-hidden className="size-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
