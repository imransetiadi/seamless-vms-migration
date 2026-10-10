// The control plane's ai/evidence.py, ported for the mock (SDD §14.4, ADV-02): the facts a strategy
// recommendation rests on, measured against one option. The case table both run keeps them equal.
import { optionLabel } from '../lib/advisor';
import { formatBytes, formatDuration } from '../lib/format';
import { guestOsOf } from '../lib/guestOs';
import { PROVIDER_KIND_LABELS } from '../lib/status';
import type { Estimate, EvidenceEffect, EvidenceItem, EvidenceSourceKind, Finding, GuestOS, Migration, Plan, ProviderKind, Strategy, VMRef } from './types';

const V2V_TEXT: Record<GuestOS['v2v'], string> = {
  supported: 'supported',
  tech_preview: 'tech preview',
  unverified: 'unverified',
  unsupported: 'unsupported',
  unknown: 'unknown',
};
/** The options virt-v2v converts. */
const CONVERSIONS: ReadonlySet<string> = new Set(['vmware_cold', 'vmware_warm']);

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** Python's datetime.isoformat() of a UTC time: `2026-10-11T22:00:00+00:00`. */
function isoUtc(at: string): string {
  const d = new Date(at);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}T${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}+00:00`;
}

/** `2026-10-11 22:00`, in UTC. */
function minuteUtc(at: string): string {
  const d = new Date(at);
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

function applies(finding: Finding, option: string): boolean {
  return finding.strategies.length === 0 || finding.strategies.includes(option as Strategy);
}

/** build_strategy_evidence: heaviest first; a fact that is unknown is left out. */
export function buildStrategyEvidence(
  vm: VMRef,
  plan: Pick<Plan, 'id' | 'source_provider_id' | 'downtime_slo_s' | 'cutover_window'>,
  providerKind: ProviderKind,
  estimates: readonly Estimate[],
  findings: readonly Finding[],
  option: string,
  earlier: Pick<Migration, 'id' | 'attempts' | 'attempt_strategies'> | null,
  migrationId: string,
): EvidenceItem[] {
  const items: EvidenceItem[] = [];
  const add = (key: string, label: string, value: EvidenceItem['value'], display: string, effect: EvidenceEffect, weight: number, kind: EvidenceSourceKind, refId: string) =>
    items.push({ key, label, value, display, effect, weight, source: { kind, ref_id: refId } });

  const found = estimates.find((e) => e.strategy === option);
  if (found) {
    add(
      'downtime_estimate',
      'Estimated downtime',
      found.downtime_s,
      `Estimated downtime ${formatDuration(found.downtime_s)}, SLO ${formatDuration(plan.downtime_slo_s)}`,
      found.meets_slo ? 'supports' : 'opposes',
      100,
      'estimate',
      migrationId,
    );
  }
  if (providerKind === 'vmware' && vm.cbt_enabled !== null && vm.cbt_enabled !== undefined) {
    const effect: EvidenceEffect = option === 'vmware_warm' ? (vm.cbt_enabled ? 'supports' : 'opposes') : 'context';
    add('cbt', 'Changed Block Tracking', vm.cbt_enabled, `Changed Block Tracking is ${vm.cbt_enabled ? 'on' : 'off'}`, effect, 90, 'inventory', vm.source_id);
  }
  if (earlier && earlier.attempts > 0) {
    const n = earlier.attempts;
    // the strategies those attempts used, as each retry recorded them - not the migration's strategy now,
    // which a later validation may have changed; data recorded before names none
    const tried: string[] = [...new Set(earlier.attempt_strategies)];
    const used = tried.length ? ` with ${tried.map((s) => optionLabel(s)).join(', ')}` : '';
    add(
      'earlier_runs',
      'Earlier attempts',
      n,
      `${n} earlier ${plural(n, 'attempt', 'attempts')}${used}`,
      tried.includes(option) ? 'opposes' : 'context',
      85,
      'run',
      earlier.id,
    );
  }
  const applying = findings.filter((f) => (f.severity === 'blocker' || f.severity === 'warning') && applies(f, option));
  const unmapped = applying.filter((f) => f.code.startsWith('MAP_'));
  add(
    'mappings',
    'Mappings',
    unmapped.length,
    unmapped.length ? `Not mapped: ${[...new Set(unmapped.map((f) => f.code))].join(', ')}` : 'Networks, flavors and volume types are mapped',
    unmapped.length ? 'opposes' : 'supports',
    80,
    'preflight',
    migrationId,
  );
  const others = applying.filter((f) => !f.code.startsWith('MAP_'));
  add(
    'findings',
    'Pre-flight findings',
    others.length,
    others.length
      ? `${others.length} ${plural(others.length, 'finding applies', 'findings apply')} to ${optionLabel(option)}: ${[...new Set(others.map((f) => f.code))].join(', ')}`
      : `No blocker or warning applies to ${optionLabel(option)}`,
    others.length ? 'opposes' : 'supports',
    75,
    'preflight',
    migrationId,
  );
  const guest = guestOsOf(vm);
  if (guest.family !== 'unknown') {
    const effect: EvidenceEffect = !CONVERSIONS.has(option)
      ? 'context'
      : guest.v2v === 'supported'
        ? 'supports'
        : guest.v2v === 'unsupported' || guest.v2v === 'unverified'
          ? 'opposes'
          : 'context';
    add('guest_os', 'Guest OS', guest.label, `${guest.label}; virt-v2v ${V2V_TEXT[guest.v2v]}`, effect, 70, 'inventory', vm.source_id);
  }
  add('disk_total', 'Disk size', vm.disk_bytes, `${formatBytes(vm.disk_bytes)} provisioned, ${formatBytes(vm.used_bytes)} used`, 'context', 60, 'inventory', vm.source_id);
  const disks = vm.disks.length;
  add('disk_count', 'Disks', disks, `${disks} ${plural(disks, 'disk', 'disks')}`, 'context', 50, 'inventory', vm.source_id);
  if (vm.change_rate_bps !== null && vm.change_rate_bps !== undefined) {
    add('write_rate', 'Guest write rate', vm.change_rate_bps, `Guest write rate ${formatBytes(vm.change_rate_bps)}/s, measured`, 'context', 40, 'inventory', vm.source_id);
  }
  const window = plan.cutover_window;
  add(
    'cutover_window',
    'Cutover window',
    window ? `${isoUtc(window.start)}/${isoUtc(window.end)}` : null,
    window ? `Cutover window: ${minuteUtc(window.start)} – ${minuteUtc(window.end)} UTC` : 'Cutover window: any time',
    'context',
    30,
    'plan',
    plan.id,
  );
  add('source_kind', 'Source platform', providerKind, `Source platform: ${PROVIDER_KIND_LABELS[providerKind] ?? providerKind}`, 'context', 20, 'provider', plan.source_provider_id);
  return items.sort((a, b) => b.weight - a.weight);
}

/** What a policy's minimum counts: the items that support or oppose (SDD §14.4). */
export function evidenceCount(items: readonly EvidenceItem[]): number {
  return items.filter((item) => item.effect !== 'context').length;
}
