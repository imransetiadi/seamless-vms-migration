/** The advisor audit trail as people read and export it (SDD §14.5, ADV-05). */
import type { AuditEventKind, Event } from '../api/types';

const ACTION_LABELS: Record<AuditEventKind, string> = {
  'recommendation.created': 'Recommendation created',
  'decision.auto_applied': 'Decision: applied automatically',
  'decision.pending_approval': 'Decision: waiting for approval',
  'decision.approved': 'Decision: approved',
  'decision.overridden': 'Decision: overridden',
  'decision.below_threshold': 'Decision: below threshold, default kept',
  'decision.fallback': 'Decision: fell back to the rules',
  'policy.created': 'Policy created',
  'policy.updated': 'Policy changed',
  'policy.deleted': 'Policy deleted',
  'migration.force_cutover': 'Cutover forced',
};

export function auditActionLabel(kind: string): string {
  return ACTION_LABELS[kind as AuditEventKind] ?? kind;
}

/** The columns the spec names for the export. */
export const AUDIT_CSV_HEADER = 'time,actor,action,target,model version';

function text(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

/** One CSV field: quoted when it must be, and never read by a spreadsheet as a formula (CWE-1236). */
function csvCell(value: string): string {
  const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** The events as CSV (RFC 4180, CRLF), one row each: time, actor, action, target, model version. */
export function auditCsv(events: Event[]): string {
  const rows = events.map((e) => [e.ts, e.actor, e.kind, text(e.data.target_id), text(e.data.model_version)].map(csvCell).join(','));
  return [AUDIT_CSV_HEADER, ...rows, ''].join('\r\n');
}
