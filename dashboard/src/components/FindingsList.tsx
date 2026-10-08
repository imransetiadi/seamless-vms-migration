import { CircleCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import { SEVERITIES, type Finding, type Severity } from '../api/types';
import { cn } from '../lib/cn';
import type { FindingGroup } from '../lib/findings';
import { SEVERITY_ORDER, severityMeta, STRATEGY_LABELS, TONE_CLASSES } from '../lib/status';
import { EmptyState } from './EmptyState';
import { SeverityBadge } from './StatusBadge';

const NOUN: Record<Severity, [string, string]> = {
  blocker: ['blocker', 'blockers'],
  warning: ['warning', 'warnings'],
  info: ['info', 'info'],
};

/** Compact per-severity counts for table cells: "1 blocker · 2 warnings". */
export function FindingsSummary({ findings }: { findings: Finding[] }) {
  const counts = SEVERITIES.map((severity) => ({ severity, n: findings.filter((f) => f.severity === severity).length })).filter((c) => c.n > 0);
  if (counts.length === 0) return <span className="text-xs text-muted-foreground">None</span>;
  return (
    <ul className="flex flex-col gap-0.5 text-xs" title={findings.map((f) => f.code).join(', ')}>
      {counts.map(({ severity, n }) => {
        const meta = severityMeta(severity);
        return (
          <li key={severity} className={cn('inline-flex items-center gap-1 whitespace-nowrap', TONE_CLASSES[meta.tone].text)}>
            <meta.icon aria-hidden className="size-3.5 shrink-0" />
            {n} {NOUN[severity][n === 1 ? 0 : 1]}
          </li>
        );
      })}
    </ul>
  );
}

export interface FindingRow extends Finding {
  vmName?: string;
  migrationId?: string;
}

/** Pre-flight findings (SDD §9.3), most severe first, each with its remediation. */
export function FindingsList({ findings, emptyText = 'No findings — pre-flight passed.' }: { findings: FindingRow[]; emptyText?: string }) {
  if (findings.length === 0) return <EmptyState icon={CircleCheck} title={emptyText} />;
  const sorted = [...findings].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || a.code.localeCompare(b.code) || (a.vmName ?? '').localeCompare(b.vmName ?? ''),
  );
  return (
    <ul className="-my-2 divide-y divide-border">
      {sorted.map((f, index) => (
        <li key={`${f.code}-${f.migrationId ?? ''}-${index}`} className="flex flex-col gap-1 py-2.5 sm:flex-row sm:gap-3">
          <div className="shrink-0 sm:w-24">
            <SeverityBadge severity={f.severity} />
          </div>
          <div className="min-w-0 flex-1 text-sm">
            <p className="flex flex-wrap items-center gap-x-2">
              <code className="text-xs font-semibold text-foreground">{f.code}</code>
              {f.vmName &&
                (f.migrationId ? (
                  <Link to={`/migrations/${f.migrationId}`} className="font-mono text-xs underline underline-offset-4">
                    {f.vmName}
                  </Link>
                ) : (
                  <span className="font-mono text-xs">{f.vmName}</span>
                ))}
            </p>
            <p className="break-words text-foreground">{f.message}</p>
            {f.remediation && <p className="break-words text-muted-foreground">Fix: {f.remediation}</p>}
            {f.strategies.length > 0 && (
              <p className="text-xs text-muted-foreground">Affects: {f.strategies.map((s) => STRATEGY_LABELS[s] ?? s).join(', ')}</p>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Plan-wide findings grouped by code: one entry per problem with the affected VMs. */
export function FindingGroupsList({ groups, emptyText = 'No findings — pre-flight passed.' }: { groups: FindingGroup[]; emptyText?: string }) {
  if (groups.length === 0) return <EmptyState icon={CircleCheck} title={emptyText} />;
  return (
    <ul className="-my-2 divide-y divide-border">
      {groups.map((g) => (
        <li key={`${g.severity}-${g.code}`} className="flex flex-col gap-1 py-2.5 sm:flex-row sm:gap-3">
          <div className="shrink-0 sm:w-24">
            <SeverityBadge severity={g.severity} />
          </div>
          <div className="min-w-0 flex-1 text-sm">
            <p className="flex flex-wrap items-baseline gap-x-2">
              <code className="text-xs font-semibold text-foreground">{g.code}</code>
              <span className="text-xs text-muted-foreground">
                {g.items.length} VM{g.items.length === 1 ? '' : 's'}
              </span>
            </p>
            {g.sameMessage && <p className="break-words text-foreground">{g.items[0]?.message}</p>}
            {g.remediation && <p className="break-words text-muted-foreground">Fix: {g.remediation}</p>}
            {g.strategies.length > 0 && (
              <p className="text-xs text-muted-foreground">Affects: {g.strategies.map((s) => STRATEGY_LABELS[s] ?? s).join(', ')}</p>
            )}
            <ul aria-label={`VMs with ${g.code}`} className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {g.items.map((item) => (
                <li key={`${item.vmName}-${item.migrationId ?? ''}`}>
                  {item.migrationId ? (
                    <Link to={`/migrations/${item.migrationId}`} className="font-mono text-xs underline underline-offset-4">
                      {item.vmName}
                    </Link>
                  ) : (
                    <span className="font-mono text-xs">{item.vmName}</span>
                  )}
                </li>
              ))}
            </ul>
            {!g.sameMessage && (
              <details className="mt-1">
                <summary className="inline-flex min-h-8 cursor-pointer items-center text-xs text-muted-foreground hover:text-foreground">
                  Details per VM
                </summary>
                <ul className="mt-1 flex flex-col gap-1">
                  {g.items.map((item) => (
                    <li key={`${item.vmName}-${item.migrationId ?? ''}`} className="break-words text-xs">
                      <span className="font-mono">{item.vmName}</span>
                      <span className="text-muted-foreground"> — {item.message}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
