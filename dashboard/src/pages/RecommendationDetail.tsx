import { History, ShieldAlert, ShieldCheck } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import { useEventTail, useRecommendation, useSnapshot } from '../api/hooks';
import type { AdvisorRecommendation, Event } from '../api/types';
import { ErrorBanner } from '../components/ErrorBanner';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { LoadingBlock } from '../components/Skeleton';
import { DecisionLine, RecommendedLine } from '../components/Recommendations';
import { Tabs } from '../components/Tabs';
import { CONFIDENCE_DEFINITION, DECISION_TYPE_LABELS, distributionRows, formatConfidence, optionLabel } from '../lib/advisor';
import { auditActionLabel } from '../lib/audit';
import { canonicalJson, sha256Hex } from '../lib/canonical';
import { formatDateTime, formatPct } from '../lib/format';
import { ADVISOR_SOURCE_LABELS } from '../lib/status';
import { usePageTitle } from '../lib/usePageTitle';

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** What an audit event changed, in the words of the feed. */
function change(e: Event): string {
  if (e.kind === 'recommendation.created') {
    const option = text(e.data.option);
    const confidence = typeof e.data.confidence === 'number' ? e.data.confidence : null;
    return option ? `${optionLabel(option)} recommended${confidence !== null ? ` at ${formatConfidence(confidence)}` : ''}` : 'Nothing recommended';
  }
  const state = (value: unknown) => {
    const s = record(value);
    const final = text(s.final_option);
    return final ? optionLabel(final) : s.status === 'pending_approval' ? 'Waiting for approval' : 'The default';
  };
  return e.data.before ? `${state(e.data.before)} → ${state(e.data.after)}` : state(e.data.after);
}

/** The full distribution, highest first, with what confidence means (ADV-03). */
function DetailsTab({ rec }: { rec: AdvisorRecommendation }) {
  const rows = distributionRows(rec);
  return (
    <div className="flex flex-col gap-3">
      <p className="max-w-prose text-sm text-muted-foreground">
        <span className="font-medium text-foreground">Confidence</span> means {CONFIDENCE_DEFINITION} The probabilities below are what Jev gave each
        option.
      </p>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{rec.recommendation.option === null ? 'The advisor gave no answer, so there is no distribution.' : 'No distribution was recorded.'}</p>
      ) : (
        <div className="table-wrap rounded-md border border-border">
          <table className="data-table">
            <caption className="sr-only">Probability Jev gave each option, highest first</caption>
            <thead>
              <tr>
                <th scope="col">Option</th>
                <th scope="col">Probability</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.option}>
                  <td>{row.label}</td>
                  <td className="num">{row.pct}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AuditTab({ rec }: { rec: AdvisorRecommendation }) {
  const scope = rec.migration_id ? { migration_id: rec.migration_id } : rec.plan_id ? { plan_id: rec.plan_id } : {};
  const events = useEventTail({ ...scope, audit: true }, 1000);
  if (rec.legacy) return <p className="text-sm text-muted-foreground">Written before the audit trail existed: this entry has no audit events.</p>;
  if (events.isPending) return <LoadingBlock rows={2} />;
  if (events.error) return <ErrorBanner error={events.error} title="The audit trail could not be loaded" onRetry={() => void events.refetch()} />;
  const mine = events.data.filter((e) => e.data.target_id === rec.id);
  if (mine.length === 0) return <p className="text-sm text-muted-foreground">No audit events for this recommendation yet.</p>;
  return (
    <div className="table-wrap rounded-md border border-border">
      <table className="data-table">
        <caption className="sr-only">Audit events of this recommendation, oldest first</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Action</th>
            <th scope="col">Actor</th>
            <th scope="col">Model version</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {mine.map((e) => (
            <tr key={e.seq}>
              <td className="whitespace-nowrap">
                <time dateTime={e.ts}>{formatDateTime(e.ts)}</time>
              </td>
              <td>{auditActionLabel(e.kind)}</td>
              <td className="font-mono text-xs">{e.actor}</td>
              <td className="font-mono text-xs">{text(e.data.model_version) || '—'}</td>
              <td>{change(e)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function InputTab({ rec }: { rec: AdvisorRecommendation }) {
  const snapshot = useSnapshot(rec.input_snapshot_sha256);
  if (!rec.input_snapshot_sha256) {
    return (
      <p className="text-sm text-muted-foreground">
        {rec.legacy ? 'Written before inputs were kept: there is no input snapshot.' : 'No input snapshot was stored for this recommendation.'}
      </p>
    );
  }
  if (snapshot.isPending) return <LoadingBlock rows={3} />;
  if (snapshot.error) return <ErrorBanner error={snapshot.error} title="The input snapshot could not be loaded" onRetry={() => void snapshot.refetch()} />;
  const s = snapshot.data;
  // re-hash what the API returned over the canonical JSON of SDD §14.5
  const recomputed = sha256Hex(canonicalJson(s.payload));
  const matches = recomputed === s.sha256 && s.sha256 === rec.input_snapshot_sha256;
  return (
    <div className="flex flex-col gap-3">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">SHA-256</dt>
        <dd className="font-mono text-xs break-all text-foreground">{s.sha256}</dd>
        <dt className="text-muted-foreground">Stored</dt>
        <dd className="text-foreground">{formatDateTime(s.created_at)}</dd>
      </dl>
      <p role="status" className="flex items-start gap-1.5 text-sm text-foreground">
        {matches ? (
          <>
            <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-status-success" />
            Matches its SHA-256: checked in your browser over its canonical JSON.
          </>
        ) : (
          <>
            <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
            Does not match its SHA-256: your browser computed <span className="font-mono text-xs break-all">{recomputed}</span>.
          </>
        )}
      </p>
      <p className="text-xs text-muted-foreground">Redacted before it was stored: passwords, tokens and keys are removed (SDD §13.4).</p>
      <pre className="max-h-[28rem] overflow-auto rounded-md border border-border bg-muted/40 p-3 text-xs whitespace-pre-wrap break-words text-foreground">
        <code>{JSON.stringify(s.payload, null, 2)}</code>
      </pre>
    </div>
  );
}

/** One recommendation: what was recommended and decided, its audit events and its input (SDD §14.5, §16). */
export default function RecommendationDetail() {
  const { recId } = useParams();
  const rec = useRecommendation(recId);
  usePageTitle(rec.data ? `Recommendation for ${rec.data.subject.name}` : 'Recommendation');
  if (rec.isPending) return <LoadingBlock label="Loading the recommendation…" rows={3} />;
  if (rec.error) {
    return (
      <>
        <PageHeader title="Recommendation" breadcrumbs={[{ label: 'Advisor', to: '/advisor' }, { label: recId ?? '' }]} />
        <ErrorBanner error={rec.error} title="This recommendation could not be loaded" onRetry={() => void rec.refetch()} />
      </>
    );
  }
  const r = rec.data;
  const d = r.decision;
  const type = DECISION_TYPE_LABELS[r.decision_type] ?? r.decision_type;
  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Advisor', to: '/advisor' }, { label: `${type}: ${r.subject.name}` }]}
        title={`${type}: ${r.subject.name}`}
        meta={
          r.legacy ? (
            <span className="inline-flex items-center gap-1 rounded-sm border border-border bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              <History aria-hidden className="size-3.5" />
              Legacy
            </span>
          ) : undefined
        }
      />
      <div className="flex flex-col gap-4">
        <Panel title="Recommendation and decision" description="The migration runs the decision, not the recommendation">
          <RecommendedLine rec={r} className="text-sm text-muted-foreground" />
          <DecisionLine rec={r} className="mt-0.5 text-sm font-medium text-foreground" />
          {d.reason_text && <p className="mt-0.5 text-xs text-muted-foreground">{d.reason_text}</p>}
          {r.legacy && r.legacy_text && <p className="mt-2 border-l-2 border-border pl-2 text-xs text-muted-foreground">Original note: {r.legacy_text}</p>}
          <dl className="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">VM</dt>
            <dd className="text-foreground">
              {r.migration_id ? (
                <Link to={`/migrations/${r.migration_id}`} className="font-mono underline underline-offset-4">
                  {r.subject.name}
                </Link>
              ) : (
                <span className="font-mono">{r.subject.name}</span>
              )}
            </dd>
            <dt className="text-muted-foreground">Model</dt>
            <dd className="text-foreground">
              {ADVISOR_SOURCE_LABELS[r.model.name] ?? r.model.name} <span className="font-mono text-xs">{r.model.version}</span>
            </dd>
            <dt className="text-muted-foreground">Decided</dt>
            <dd className="text-foreground">
              {formatDateTime(d.decided_at)} by <span className="font-mono text-xs">{`${d.actor.kind}:${d.actor.id}`}</span>
            </dd>
            <dt className="text-muted-foreground">Policy</dt>
            <dd className="text-foreground">
              {d.policy_id ?? 'none'}
              {d.threshold_used !== null && `, threshold ${formatPct(d.threshold_used * 100, 0)}`}
            </dd>
            <dt className="text-muted-foreground">Id</dt>
            <dd className="font-mono text-xs break-all text-foreground">{r.id}</dd>
          </dl>
        </Panel>
        <Panel title="Records" description="The full distribution, the audit events and the input this recommendation rests on">
          <Tabs
            label="Recommendation records"
            tabs={[
              { id: 'details', label: 'Details', content: <DetailsTab rec={r} /> },
              { id: 'audit', label: 'Audit', content: <AuditTab rec={r} /> },
              { id: 'input', label: 'Input', content: <InputTab rec={r} /> },
            ]}
          />
        </Panel>
      </div>
    </>
  );
}
