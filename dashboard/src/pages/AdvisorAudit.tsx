import { Download, FileJson, ScrollText, SearchX } from 'lucide-react';
import { useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useEventTail } from '../api/hooks';
import { AUDIT_EVENT_KINDS } from '../api/types';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { SelectField } from '../components/Field';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { auditActionLabel, auditCsv } from '../lib/audit';
import { formatDateTime, formatNumber } from '../lib/format';
import { usePageTitle } from '../lib/usePageTitle';

/** The newest audit events one request returns (SDD §12). */
const AUDIT_KEEP = 1000;

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function download(name: string, body: string, type: string) {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

/** The advisor's audit trail: who decided what, when, with which model version (SDD §14.5, §16). */
export default function AdvisorAudit() {
  usePageTitle('Advisor audit');
  const searchId = useId();
  const audit = useEventTail({ audit: true }, AUDIT_KEEP);
  const [action, setAction] = useState('all');
  const [actor, setActor] = useState('all');
  const [query, setQuery] = useState('');

  const events = useMemo(() => [...(audit.data ?? [])].sort((a, b) => b.seq - a.seq), [audit.data]);
  // the VM of a recommendation, as its recommendation.created event names it
  const names = useMemo(
    () =>
      new Map(
        events
          .filter((e) => e.kind === 'recommendation.created')
          .map((e) => [text(e.data.target_id), text((e.data.subject as { name?: unknown } | undefined)?.name)]),
      ),
    [events],
  );
  const actors = useMemo(() => [...new Set(events.map((e) => e.actor))].sort(), [events]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter(
      (e) =>
        (action === 'all' || e.kind === action) &&
        (actor === 'all' || e.actor === actor) &&
        (!q || [e.message, e.actor, e.kind, text(e.data.target_id), names.get(text(e.data.target_id)) ?? ''].some((v) => v.toLowerCase().includes(q))),
    );
  }, [events, action, actor, query, names]);

  // downloads read oldest first, like `seamless events export`
  const oldestFirst = () => [...shown].reverse();
  const stamp = () => new Date().toISOString().slice(0, 19).replace(/:/g, '');
  const downloadReason = audit.error ? 'The audit trail could not be loaded.' : !audit.data ? 'The audit trail is loading.' : shown.length === 0 ? 'No audit events are shown.' : null;

  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Advisor', to: '/advisor' }, { label: 'Audit trail' }]}
        title="Advisor audit"
        description="Who decided what, when, and with which model version. The trail is append-only: the API can neither change nor delete it."
        actions={
          <>
            <Button size="lg" icon={Download} disabledReason={downloadReason} onClick={() => download(`seamless-advisor-audit-${stamp()}.csv`, auditCsv(oldestFirst()), 'text/csv')}>
              Download CSV
            </Button>
            <Button
              size="lg"
              icon={FileJson}
              disabledReason={downloadReason}
              onClick={() => download(`seamless-advisor-audit-${stamp()}.json`, `${JSON.stringify(oldestFirst(), null, 2)}\n`, 'application/json')}
            >
              Download JSON
            </Button>
          </>
        }
      />

      <div className="mb-3 grid grid-cols-2 gap-2 md:flex md:flex-wrap md:items-end">
        <div className="col-span-2 min-w-0 md:w-72">
          <label htmlFor={searchId} className="field-label">
            Search
          </label>
          <input id={searchId} type="search" className="input" placeholder="VM, recommendation id, actor or text" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <SelectField
          label="Action"
          className="md:w-72"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          options={[{ value: 'all', label: 'All actions' }, ...AUDIT_EVENT_KINDS.map((kind) => ({ value: kind, label: auditActionLabel(kind) }))]}
        />
        <SelectField label="Actor" className="md:w-60" value={actor} onChange={(e) => setActor(e.target.value)} options={[{ value: 'all', label: 'All actors' }, ...actors.map((a) => ({ value: a, label: a }))]} />
      </div>

      <Panel title="Audit events" description={`Newest first; the newest ${AUDIT_KEEP} events the API returns`}>
        {/* a live region already on the page: what the filters show is read out, none too (SDD §16) */}
        <p role="status" className="mb-2 text-xs text-muted-foreground empty:hidden">
          {events.length > 0 ? `Showing ${formatNumber(shown.length)} of ${formatNumber(events.length)} audit events` : ''}
        </p>
        {audit.isPending ? (
          <LoadingBlock rows={5} />
        ) : audit.error ? (
          <ErrorBanner error={audit.error} title="The audit trail could not be loaded" onRetry={() => void audit.refetch()} />
        ) : events.length === 0 ? (
          <EmptyState icon={ScrollText} title="No audit events yet" description="Validating a plan records each recommendation and its decision here." />
        ) : shown.length === 0 ? (
          <EmptyState icon={SearchX} title="No audit events match" description="Change the search or the filters." />
        ) : (
          <div className="table-wrap rounded-md border border-border">
            <table className="data-table">
              <caption className="sr-only">Advisor audit events, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Action</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Target</th>
                  <th scope="col">Model version</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((e) => {
                  const target = text(e.data.target_id);
                  // a Force cutover's target is the migration it forced (SDD §14.5)
                  const forced = e.kind === 'migration.force_cutover';
                  // a policy change names the policy (SDD §14.5); the policies page lists it
                  const policyName = text((e.data.after as { name?: unknown } | null)?.name) || text((e.data.before as { name?: unknown } | null)?.name);
                  return (
                    <tr key={e.seq}>
                      <td className="whitespace-nowrap">
                        <time dateTime={e.ts}>{formatDateTime(e.ts)}</time>
                      </td>
                      <td>
                        {auditActionLabel(e.kind)}
                        {/* a Force cutover's reason goes with it (SDD §14.5, ADV-16) */}
                        {forced && text(e.data.reason) && <p className="mt-0.5 wrap-break-word text-xs text-muted-foreground">{text(e.data.reason)}</p>}
                      </td>
                      <td className="font-mono text-xs">{e.actor}</td>
                      <td>
                        {target && e.kind.startsWith('policy.') ? (
                          <Link to="/advisor/policies" className="underline underline-offset-4">
                            {policyName || target}
                          </Link>
                        ) : target && forced ? (
                          <Link to={`/migrations/${encodeURIComponent(target)}`} className="font-mono underline underline-offset-4">
                            {text(e.data.vm) || target}
                          </Link>
                        ) : target ? (
                          <Link to={`/advisor/recommendations/${encodeURIComponent(target)}`} className="font-mono underline underline-offset-4">
                            {names.get(target) || target}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="font-mono text-xs">{text(e.data.model_version) || '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
