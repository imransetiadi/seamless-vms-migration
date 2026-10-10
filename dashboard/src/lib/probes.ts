import { CircleCheck, CircleHelp, CircleX, type LucideIcon } from 'lucide-react';
import { MAX_PROBE_PATH, MAX_PROBES_PER_VM, type AppProbe, type Plan, type ProbeKind, type ProbeResult } from '../api/types';
import type { Tone } from './phase';

/** "TCP 5432", "HTTP 9200 /", "HTTPS 443 /health (204)" — as the control plane writes them (SDD §9.6). */
export function describeProbe(probe: AppProbe): string {
  if (probe.kind === 'tcp') return `TCP ${probe.port}`;
  const status = probe.expect_status !== null ? ` (${probe.expect_status})` : '';
  return `${probe.kind.toUpperCase()} ${probe.port} ${probe.path}${status}`;
}

export function describeProbes(probes: readonly AppProbe[]): string {
  return probes.map(describeProbe).join(', ');
}

/** The probes a VM's verification runs: its own (`probe_overrides`), else the plan's (SDD §7.5). */
export function probesFor(vmId: string, plan: Pick<Plan, 'verification' | 'probe_overrides'>): { probes: AppProbe[]; source: 'override' | 'plan' } {
  const own = plan.probe_overrides[vmId];
  return own !== undefined ? { probes: own, source: 'override' } : { probes: plan.verification.app_probes, source: 'plan' };
}

/** The plan's `probe_overrides` with a VM's own probes set, or removed with `null` (SDD §7.5). */
export function withVmProbes(plan: Pick<Plan, 'probe_overrides'>, vmId: string, probes: AppProbe[] | null): Record<string, AppProbe[]> {
  const { [vmId]: _previous, ...others } = plan.probe_overrides;
  return probes === null ? others : { ...others, [vmId]: probes };
}

/** What the API refuses in a probe list (SDD §12), in words; empty when it takes the list. */
export function probeProblems(probes: readonly AppProbe[]): string[] {
  const problems: string[] = [];
  probes.forEach((p, i) => {
    const n = i + 1;
    if (!Number.isInteger(p.port) || p.port < 1 || p.port > 65535) problems.push(`Probe ${n}: use a port from 1 to 65535.`);
    if (p.kind !== 'tcp' && (!p.path.startsWith('/') || p.path.length > MAX_PROBE_PATH)) {
      problems.push(`Probe ${n}: the path starts with '/' (at most ${MAX_PROBE_PATH} characters).`);
    }
    if (p.kind !== 'tcp' && p.expect_status !== null && (!Number.isInteger(p.expect_status) || p.expect_status < 100 || p.expect_status > 599)) {
      problems.push(`Probe ${n}: the expected status is from 100 to 599.`);
    }
  });
  if (probes.length > MAX_PROBES_PER_VM) problems.push(`At most ${MAX_PROBES_PER_VM} probes for one VM.`);
  return problems;
}

/** A probe as the editor holds it: numbers as typed. */
export interface ProbeDraft {
  kind: ProbeKind;
  port: string;
  path: string;
  expectStatus: string;
}

export function toDraft(probe: AppProbe): ProbeDraft {
  return { kind: probe.kind, port: String(probe.port), path: probe.path, expectStatus: probe.expect_status === null ? '' : String(probe.expect_status) };
}

export const NEW_PROBE: ProbeDraft = { kind: 'tcp', port: '', path: '/', expectStatus: '' };

/** The probes the drafts describe, and what the API would refuse in them. */
export function parseDrafts(drafts: readonly ProbeDraft[]): { probes: AppProbe[]; problems: string[] } {
  const probes = drafts.map((d): AppProbe => {
    const http = d.kind !== 'tcp';
    const port = d.port.trim() ? Number(d.port) : Number.NaN;
    return {
      kind: d.kind,
      port,
      path: http ? d.path.trim() : '/',
      expect_status: http && d.expectStatus.trim() ? Number(d.expectStatus) : null,
    };
  });
  return { probes, problems: probeProblems(probes) };
}

export const PROBE_RESULT_META: Record<ProbeResult['result'], { label: string; tone: Tone; icon: LucideIcon }> = {
  passed: { label: 'Passed', tone: 'success', icon: CircleCheck },
  failed: { label: 'Failed', tone: 'danger', icon: CircleX },
  // not a failure (SDD §7.5): the control plane could not reach the address, a person reviews it
  unreachable: { label: 'Unreachable', tone: 'warning', icon: CircleHelp },
};
