import { CREDENTIAL_CODES } from '../api/findingCatalog';
import type { Finding, Severity, Strategy } from '../api/types';
import { SEVERITY_ORDER } from './status';

export interface FindingGroupItem {
  vmName: string;
  migrationId?: string;
  message: string;
}

export interface FindingGroup {
  /** What tells the group apart: its severity and code, and a credential finding's message too (SDD §16). */
  key: string;
  code: string;
  severity: Severity;
  remediation: string | null;
  strategies: Strategy[];
  items: FindingGroupItem[];
  /** Every VM reports the same message (show it once). */
  sameMessage: boolean;
}

/** Groups plan-wide findings by code so a finding that hits many VMs reads as one line. A credential finding is about one
 * project, which its message names with its provider: it is grouped by its message too, one entry per project (SDD §16, §27.7,
 * plan task TC6). */
export function groupFindings(rows: Array<Finding & { vmName?: string; migrationId?: string }>): FindingGroup[] {
  const groups = new Map<string, FindingGroup>();
  for (const f of rows) {
    const key = CREDENTIAL_CODES.has(f.code) ? `${f.severity}:${f.code}:${f.message}` : `${f.severity}:${f.code}`;
    const group =
      groups.get(key) ??
      ({ key, code: f.code, severity: f.severity, remediation: f.remediation, strategies: f.strategies, items: [], sameMessage: true } satisfies FindingGroup);
    group.items.push({ vmName: f.vmName ?? '—', migrationId: f.migrationId, message: f.message });
    groups.set(key, group);
  }
  for (const group of groups.values()) {
    group.items.sort((a, b) => a.vmName.localeCompare(b.vmName, undefined, { numeric: true }));
    group.sameMessage = group.items.every((i) => i.message === group.items[0]?.message);
  }
  return [...groups.values()].sort(
    (a, b) =>
      SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || b.items.length - a.items.length || a.code.localeCompare(b.code) || a.key.localeCompare(b.key),
  );
}
