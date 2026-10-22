import type { Hook, HookPoint } from '../api/types';

/** The points a plan runs hooks at, in the cutover's order, with their names on the form (SDD §8.2, §16). */
export const HOOK_POINTS: ReadonlyArray<readonly [HookPoint, string]> = [
  ['pre_stop', 'Before the stop'],
  ['post_create', 'After the server is created'],
  ['post_verify', 'After the verification'],
  ['on_rollback', 'On rollback'],
  ['on_finalize', 'On finalize'],
];

/** The names of the hook kinds (SDD §8.2, §16). */
export const HOOK_KIND_LABELS: Readonly<Record<Hook['kind'], string>> = { playbook: 'Playbook', webhook: 'Webhook' };

/** A hook kind's name as an event's data gives it (SDD §8.2): a kind it does not know as it is, none as none. */
export function hookKindLabel(kind: unknown): string | null {
  if (kind === 'playbook' || kind === 'webhook') return HOOK_KIND_LABELS[kind];
  return typeof kind === 'string' && kind ? kind : null;
}

/** A DNS target as the form edits it (SDD §4.2 DnsTarget, §8.4). */
export interface DnsTargetDraft {
  vmId: string;
  kind: 'designate' | 'rfc2136';
  zone: string;
  record: string;
  server: string;
  tsigSecret: string;
}

/** An Octavia pool mapping as the form edits it (SDD §4.2 lb_pools, §8.5). */
export interface PoolMappingDraft {
  source: string;
  destination: string;
}

export const NEW_DNS_TARGET: DnsTargetDraft = { vmId: '', kind: 'designate', zone: '', record: '', server: '', tsigSecret: '' };

/** The network cut-over a form holds (SDD §4.2, §16, plan task N12b). */
export interface CutoverDraft {
  floatingIps: 'auto' | 'keep_address';
  hooks: Partial<Record<HookPoint, string[]>>;
  dnsTargets: DnsTargetDraft[];
  poolMappings: PoolMappingDraft[];
}

/**
 * Whether the form shows and checks a DNS target: a VM taken out of the plan takes its target with it, kept for the
 * VM's return as its strategy override is, and a target whose VM is not chosen yet stays (SDD §16).
 */
export function targetInPlan(target: DnsTargetDraft, selected: ReadonlySet<string>): boolean {
  return !target.vmId || selected.has(target.vmId);
}

/** What the form refuses in a network cut-over, the first problem at a time, as the API would (SDD §4.2, §12). */
export function cutoverProblem(value: CutoverDraft, selected: ReadonlySet<string>): string | null {
  const seen = new Set<string>();
  for (const [i, target] of value.dnsTargets.filter((t) => targetInPlan(t, selected)).entries()) {
    const n = i + 1;
    if (!target.vmId || !target.zone.trim() || !target.record.trim()) return `DNS target ${n}: choose a VM, and enter its zone and record.`;
    if (seen.has(target.vmId)) return `DNS target ${n}: its VM has a DNS target already.`;
    seen.add(target.vmId);
    if (target.kind === 'rfc2136' && (!target.server.trim() || !target.tsigSecret.trim())) return `DNS target ${n}: an RFC 2136 target needs its server and its TSIG secret.`;
  }
  const sources = new Set<string>();
  for (const [i, mapping] of value.poolMappings.entries()) {
    const n = i + 1;
    if (!mapping.source.trim() || !mapping.destination.trim()) return `Pool mapping ${n}: enter the source pool's id and the destination pool's id.`;
    if (sources.has(mapping.source.trim())) return `Pool mapping ${n}: its source pool is mapped already.`;
    sources.add(mapping.source.trim());
  }
  return null;
}

/** A network cut-over as the API takes it (SDD §4.2): a point without hooks is left out, an empty text is none. */
export function cutoverBody(value: CutoverDraft, selected: ReadonlySet<string>) {
  const text = (s: string) => s.trim();
  return {
    hooks: Object.fromEntries(HOOK_POINTS.flatMap(([point]) => ((value.hooks[point] ?? []).length ? [[point, value.hooks[point]!]] : []))),
    floating_ips: value.floatingIps,
    // a VM taken out of the plan takes its target with it (SDD §16)
    dns_targets: Object.fromEntries(
      value.dnsTargets
        .filter((target) => selected.has(target.vmId))
        .map((target) => [
          target.vmId,
          {
            kind: target.kind,
            zone: text(target.zone),
            record: text(target.record),
            server: target.kind === 'rfc2136' ? text(target.server) : null,
            tsig_secret: target.kind === 'rfc2136' ? text(target.tsigSecret) : null,
          },
        ]),
    ),
    lb_pools: Object.fromEntries(value.poolMappings.map((mapping) => [text(mapping.source), text(mapping.destination)])),
  };
}
