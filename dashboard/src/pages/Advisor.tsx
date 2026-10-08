import { Bot, BrainCircuit, CircleCheck, CircleSlash, CircleX, Search, SearchX } from 'lucide-react';
import { useId, useMemo, useState, type FormEvent } from 'react';
import { useAdvisorStatus, useMigrations, useSimilarIncidents } from '../api/hooks';
import { useRole } from '../api/session';
import { AdvisorNotes, type AdvisorNoteRow } from '../components/AdvisorNotes';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { StatusBadge } from '../components/StatusBadge';
import { formatPct } from '../lib/format';
import { hasRole } from '../lib/roles';
import { usePageTitle } from '../lib/usePageTitle';

const SUGGESTIONS = ['kernel panic after import', 'warm sync does not converge', 'volume type not mapped', 'MTU after cutover'];

function ServiceCard({
  title,
  icon: Icon,
  state,
  lines,
  lastError,
}: {
  title: string;
  icon: typeof Bot;
  state: 'available' | 'unavailable' | 'off';
  lines: Array<[string, string]>;
  lastError?: string | null;
}) {
  const badge =
    state === 'available'
      ? { tone: 'success' as const, icon: CircleCheck, label: 'Available' }
      : state === 'off'
        ? { tone: 'neutral' as const, icon: CircleSlash, label: 'Off' }
        : { tone: 'danger' as const, icon: CircleX, label: 'Unavailable' };
  return (
    <section aria-label={title} className="card flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
          <Icon aria-hidden className="size-5 text-muted-foreground" />
          {title}
        </h2>
        <StatusBadge tone={badge.tone} icon={badge.icon} label={badge.label} size="md" />
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
        {lines.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="text-foreground">{v}</dd>
          </div>
        ))}
      </dl>
      {lastError && (
        <p className="break-words rounded-md border border-status-warning/40 bg-status-warning/10 px-2.5 py-1.5 text-xs text-foreground">
          Last error: {lastError}
        </p>
      )}
    </section>
  );
}

export default function Advisor() {
  usePageTitle('Advisor');
  const queryId = useId();
  const status = useAdvisorStatus();
  const migrations = useMigrations();
  const search = useSimilarIncidents();
  const role = useRole();
  const canSearch = hasRole(role, 'operator');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState('5');
  const [touched, setTouched] = useState(false);

  const s = status.data;
  const memoryEnabled = s?.memory.enabled ?? false;

  const notes = useMemo<AdvisorNoteRow[]>(
    () =>
      (migrations.data ?? [])
        .flatMap((m) => m.advisor_notes.map((n) => ({ ...n, vmName: m.vm.name, migrationId: m.id })))
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
        .slice(0, 20),
    [migrations.data],
  );

  const run = (text: string) => {
    setTouched(true);
    if (!text.trim() || !canSearch || !memoryEnabled) return;
    search.mutate({ query: text.trim(), limit: Number(limit) });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    run(query);
  };


  return (
    <>
      <PageHeader
        title="Advisor"
        description="Jev gives bounded, evidence-based judgments (strategy ties, workload tiers, verification review, console screening); agentmemory recalls what happened in past migrations. Both are optional and advisory."
      />

      {status.isPending && <LoadingBlock label="Checking advisor services…" rows={2} />}
      {status.error && <ErrorBanner error={status.error} title="Advisor status is unavailable" onRetry={() => void status.refetch()} />}
      {s && (
        <div className="mb-4 grid gap-4 md:grid-cols-2">
          <ServiceCard
            title="Jev"
            icon={Bot}
            state={s.jev.mode === 'off' ? 'off' : s.jev.available ? 'available' : 'unavailable'}
            lines={[
              ['Mode', s.jev.mode],
              ['Used for', 'strategy ties, classification, verification, screening'],
            ]}
            lastError={s.jev.last_error}
          />
          <ServiceCard
            title="agentmemory"
            icon={BrainCircuit}
            state={!s.memory.enabled ? 'off' : s.memory.available ? 'available' : 'unavailable'}
            lines={[
              ['Enabled', s.memory.enabled ? 'Yes' : 'No'],
              ['Used for', 'similar incidents, lessons from completed and failed runs'],
            ]}
            lastError={s.memory.last_error}
          />
        </div>
      )}

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <Panel title="Similar incidents" description="Search agentmemory for past failures and their fixes">
          <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_7rem_auto] sm:items-end">
              <div className="min-w-0">
                <label htmlFor={queryId} className="field-label">
                  Describe the problem
                </label>
                <input
                  id={queryId}
                  type="search"
                  className="input"
                  placeholder="e.g. warm sync does not converge on PostgreSQL"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  aria-invalid={touched && !query.trim() ? true : undefined}
                  aria-describedby={touched && !query.trim() ? `${queryId}-error` : undefined}
                />
              </div>
              <SelectField
                label="Results"
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
                options={['3', '5', '10'].map((v) => ({ value: v, label: v }))}
              />
              <Button
                type="submit"
                size="lg"
                variant="primary"
                icon={Search}
                loading={search.isPending}
                disabledReason={canSearch ? (memoryEnabled ? null : 'agentmemory is not enabled.') : 'Searching requires the operator role.'}
              >
                Search
              </Button>
            </div>
            {touched && !query.trim() && (
              <p id={`${queryId}-error`} className="field-error">
                Enter a few words about the failure to search for.
              </p>
            )}
            {canSearch && memoryEnabled && (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>Try:</span>
                {SUGGESTIONS.map((text) => (
                  <button
                    key={text}
                    type="button"
                    onClick={() => {
                      setQuery(text);
                      run(text);
                    }}
                    className="min-h-8 cursor-pointer rounded-full border border-border px-2.5 text-foreground transition-colors duration-200 hover:bg-muted"
                  >
                    {text}
                  </button>
                ))}
              </div>
            )}
          </form>
          <p role="status" className="sr-only">
            {search.data ? `${search.data.hits.length} similar incident${search.data.hits.length === 1 ? '' : 's'} found.` : ''}
          </p>
          <div className="mt-4">
            {search.error && <ErrorBanner error={search.error} title="The search failed" />}
            {search.data && search.data.hits.length === 0 && <EmptyState icon={SearchX} title="No similar incidents found" />}
            {search.data && search.data.hits.length > 0 && (
              <ol aria-label="Similar incidents" className="flex flex-col gap-2">
                {search.data.hits.map((hit, i) => (
                  <li key={`${hit.title}-${i}`} className="rounded-md border border-border p-3 text-sm">
                    <p className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-medium text-foreground">{hit.title}</span>
                      {typeof hit.score === 'number' && <span className="num text-xs text-muted-foreground">match {formatPct(hit.score * 100, 0)}</span>}
                    </p>
                    <p className="mt-1 break-words text-muted-foreground">{hit.content}</p>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </Panel>

        <Panel title="Recent advisor notes" description="Across all migrations, newest first">
          {migrations.isPending ? <LoadingBlock rows={4} /> : <AdvisorNotes notes={notes} emptyText="No advisor notes yet." />}
        </Panel>
      </div>
    </>
  );
}
