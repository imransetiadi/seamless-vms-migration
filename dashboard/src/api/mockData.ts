/**
 * Deterministic fixture data for the in-browser mock adapter (VITE_SEAMLESS_MOCK=1).
 * Covers every Phase, Strategy, PlanStatus, provider status, finding Severity and advisor note
 * kind/source so the whole UI can be exercised without the control plane.
 * Estimates follow SDD §9.1, eligibility §9.2 and findings a subset of §9.3.
 */
import type {
  AdvisorNote,
  AdvisorPolicy,
  AdvisorRecommendation,
  AdvisorSnapshot,
  ConvergedBy,
  DestinationInventory,
  DestinationNetwork,
  DestinationSubnet,
  Disk,
  Estimate,
  Event,
  EvidenceItem,
  Finding,
  Migration,
  Nic,
  Phase,
  PhaseChange,
  Plan,
  Provider,
  ProviderKind,
  ReasonCode,
  SimilarIncident,
  Strategy,
  SyncPass,
  VerificationRecord,
  VMRef,
} from './types';
import { canonicalJson, sha256Hex } from '../lib/canonical';
import { describeProbes, probesFor } from '../lib/probes';
import { guestOsOf, identifyGuestOs } from '../lib/guestOs';
import { addressRefusal, canonicalIp, ipInCidr, nicOverride, nicTarget, noHostAddress } from '../lib/nics';
import { networkList, portRefusal, resolveNetwork, subnetsOf } from '../lib/networks';
import { describePorts, portsWithoutOneFixedIp } from '../lib/readiness';
import { resolveDestination, splitHost, storageBackends } from '../lib/storage';
import { buildStrategyEvidence } from './mockEvidence';
import { derivePlanStatus } from './mockPlanStatus';
import { isDatabase, suggestProbes } from './mockProbes';

export const GiB = 1024 ** 3;
export const MiB = 1024 ** 2;

/** `SEAMLESS_JEV_MIN_CONFIDENCE`'s default: the global default policy's threshold (SDD §14.4). */
export const JEV_MIN_CONFIDENCE = 0.6;
export const GLOBAL_DEFAULT_POLICY = 'global-default';
/** What Jev reports as its model in a jev_decide answer. */
export const JEV_MODEL = 'jev-1.13.0';

/** The decisions validation takes on a strategy recommendation (SDD §14.4, §14.6). */
export type StrategyDecision = 'auto_applied' | 'below_threshold' | 'fallback_rules' | 'pending_approval';

const REASON_OF: Record<StrategyDecision, ReasonCode> = {
  auto_applied: 'ABOVE_THRESHOLD_AUTO_APPLY',
  below_threshold: 'BELOW_THRESHOLD_DEFAULT_KEPT',
  fallback_rules: 'ADVISOR_UNAVAILABLE_FALLBACK',
  pending_approval: 'POLICY_REQUIRES_APPROVAL',
};

/** What the mock's memory knew when the fixtures' recommendations were recorded (SDD §14.3, ADV-02). */
const FIXTURE_INCIDENTS: Partial<Record<string, SimilarIncident[]>> = {
  warm: [
    { title: 'Warm pass never converged on a busy PostgreSQL VM', content: 'Change rate (~60 MiB/s) exceeded the link share. Scheduled cutover off-peak and raised max_sync_passes to 8.', score: 0.82 },
    { title: 'sshd bound to the old fixed IP after migration', content: 'Remove ListenAddress from sshd_config or use 0.0.0.0; verification tcp:22 then passed.', score: 0.61 },
  ],
  vmware_warm: [
    { title: 'VMware CBT reset after snapshot consolidation', content: 'Consolidation reset CBT; vmware_warm restarted with a full pass. Consolidate snapshots before pre-copy.', score: 0.88 },
  ],
};

/** The demo policy of the spec (ADV-04) for the mock's DC1 source provider (SDD §14.6). */
export const FINANCE_POLICY_ID = 'pol-finance';
/** The fixtures' plans were validated by the operator persona, and their cutovers requested by the admin one (ADV-17). */
const VALIDATED_BY = 'bayu';
const REQUESTED_BY = 'rina';

export interface StrategyRecommendationInput {
  id: string;
  at: string;
  migration: Pick<Migration, 'id' | 'plan_id' | 'vm'>;
  providerId: string;
  /** What Jev recommended; null with `fallback` when it gave no answer. */
  option: string | null;
  confidence: number | null;
  /** The options Jev was offered: the confidence it did not give the option is spread over the others. */
  options: readonly string[];
  status: StrategyDecision;
  /** The deterministic choice, which the decision keeps unless the advice is applied. */
  deterministic: Strategy;
  /** Why there was no usable advice (fallback). */
  fallback?: string;
  /** The input snapshot the recommendation rests on (SDD §14.5). */
  snapshotSha256: string | null;
  /** The policy that decided (default the built-in global default, SDD §14.6). */
  policyId?: string;
  /** Its threshold (default SEAMLESS_JEV_MIN_CONFIDENCE's). */
  threshold?: number;
  /** The user whose plan validation recorded it (SDD §14.6): never the one who decides it. */
  requestedBy?: string | null;
  /** Why a decision waits when it is not the policy's mode: too little evidence (SDD §14.4, ADV-02). */
  reasonCode?: ReasonCode;
  /** The deciding policy's rules for a person's decision (SDD §14.6); none: the policy in force applies, as for data recorded before. */
  rules?: { require_distinct_approver: boolean; require_override_reason: boolean };
  /** The option the evidence is measured against, and the evidence (ADV-02). */
  evidenceOption?: string | null;
  evidence?: EvidenceItem[];
  similarIncidents?: SimilarIncident[];
}

/** A strategy recommendation and its decision as the API stores them (SDD §4.2, §14.4). */
export function strategyRecommendation(input: StrategyRecommendationInput): AdvisorRecommendation {
  const { option, confidence, status } = input;
  const others = input.options.filter((o) => o !== option);
  const distribution: Record<string, number> =
    option === null || confidence === null
      ? {}
      : Object.fromEntries([[option, confidence], ...others.map((o) => [o, Math.round(((1 - confidence) / others.length) * 100) / 100])]);
  const byPolicy = status !== 'fallback_rules';
  const policyId = input.policyId ?? GLOBAL_DEFAULT_POLICY;
  return {
    id: input.id,
    created_at: input.at,
    decision_type: 'migration_strategy',
    subject: { kind: 'vm', id: input.migration.vm.source_id, name: input.migration.vm.name },
    plan_id: input.migration.plan_id,
    migration_id: input.migration.id,
    provider_id: input.providerId,
    model: option === null && confidence === null && input.fallback
      ? { name: 'rules', version: 'rules', deployment: 'unknown', endpoint_host: null }
      : { name: 'jev', version: JEV_MODEL, deployment: 'remote', endpoint_host: null },
    recommendation: { option, confidence, distribution },
    input_snapshot_sha256: input.snapshotSha256,
    requested_by: input.requestedBy ?? null,
    decision: {
      final_option: status === 'auto_applied' ? option : status === 'pending_approval' ? null : input.deterministic,
      status,
      reason_code: input.reasonCode ?? REASON_OF[status],
      reason_text: status === 'fallback_rules' ? (input.fallback ?? null) : null,
      actor: byPolicy ? { kind: 'policy', id: policyId } : { kind: 'system', id: 'fallback' },
      policy_id: byPolicy ? policyId : null,
      threshold_used: byPolicy ? (input.threshold ?? JEV_MIN_CONFIDENCE) : null,
      decided_at: input.at,
      require_distinct_approver: byPolicy && input.rules ? input.rules.require_distinct_approver : null,
      require_override_reason: byPolicy && input.rules ? input.rules.require_override_reason : null,
    },
    legacy: false,
    legacy_text: null,
    evidence_option: input.evidenceOption ?? null,
    evidence: input.evidence ?? [],
    similar_incidents: input.similarIncidents ?? [],
    reasons: [],
  };
}

type AuditEvent = Pick<Event, 'kind' | 'message' | 'actor' | 'data'>;

function stateOf(d: AdvisorRecommendation['decision']) {
  return { status: d.status, final_option: d.final_option, reason_code: d.reason_code, policy_id: d.policy_id, threshold_used: d.threshold_used };
}

/** The event of the status a decision took, with what it replaced (SDD §14.5). */
export function decisionEventOf(rec: AdvisorRecommendation, before: AdvisorRecommendation['decision'] | null): AuditEvent {
  const d = rec.decision;
  const actor = `${d.actor.kind}:${d.actor.id}`;
  return {
    kind: d.status === 'fallback_rules' ? 'decision.fallback' : `decision.${d.status}`,
    message: `${rec.subject.name}: decision ${d.final_option ?? 'pending'} (${d.status}) by ${actor}`,
    actor,
    data: { target_id: rec.id, model_version: rec.model.version, before: before ? stateOf(before) : null, after: stateOf(d) },
  };
}

/** A person's decision on a recommendation: approve its option or override it with another (SDD §14.6). */
export function humanDecision(
  rec: AdvisorRecommendation,
  action: { kind: 'approve'; user: string } | { kind: 'override'; user: string; option: string; reason: string | null },
  at: string,
): AdvisorRecommendation {
  const approve = action.kind === 'approve';
  return {
    ...rec,
    decision: {
      final_option: approve ? rec.recommendation.option : action.option,
      status: approve ? 'approved' : 'overridden',
      reason_code: approve ? 'APPROVED_BY_USER' : 'OVERRIDDEN_BY_USER',
      reason_text: approve ? null : action.reason,
      actor: { kind: 'user', id: action.user },
      policy_id: rec.decision.policy_id ?? GLOBAL_DEFAULT_POLICY,
      threshold_used: null,
      decided_at: at,
    },
  };
}

/** The audit events the API emits for a recorded recommendation: created, then its decision (SDD §14.5). */
export function auditEventsOf(rec: AdvisorRecommendation): AuditEvent[] {
  const { option, confidence } = rec.recommendation;
  return [
    {
      kind: 'recommendation.created',
      message: `${rec.subject.name}: recommended ${option ?? 'nothing'} (${confidence === null ? 'no confidence' : `${Math.round(confidence * 100)}%`})`,
      actor: 'system:advisor',
      data: {
        target_id: rec.id,
        model_version: rec.model.version,
        decision_type: rec.decision_type,
        subject: rec.subject,
        option,
        confidence,
        snapshot_sha256: rec.input_snapshot_sha256,
      },
    },
    decisionEventOf(rec, null),
  ];
}

/** The API's STRATEGY_REQUIREMENTS. */
const STRATEGY_REQUIREMENTS = ['Estimated downtime is within the SLO', 'The source VM keeps running until cutover'];

/** One-line strategy descriptions offered to Jev as candidates (demo copy of the API's). */
const CANDIDATE_DESCRIPTIONS: Record<Strategy, string> = {
  cold: 'Stop the VM, copy every disk, boot it on RHOSO: downtime grows with the used data.',
  warm: 'Copy snapshots while the VM runs, then stop it and sync only changed blocks.',
  storage_handover: 'Hand the volumes over in the shared storage without copying: a metadata-only cutover.',
  vmware_cold: 'Stop the VMware VM and convert every disk with virt-v2v.',
  vmware_warm: 'Copy with CBT while the VMware VM runs, then a final delta after shutdown.',
};

/** The advisor's input for a strategy choice, as the API snapshots it (SDD §14.5): the jev_decide arguments and the scope. */
export function strategySnapshot(input: {
  vm: VMRef;
  plan: Plan;
  estimates: Estimate[];
  findings: Finding[];
  scope: readonly string[];
  deterministic: Strategy;
  at: string;
}): AdvisorSnapshot {
  const { vm, plan, estimates, findings } = input;
  const gib = (bytes: number) => `${(bytes / GiB).toFixed(1)} GiB`;
  const disks = vm.disks
    .map((d) => `${d.kind} ${d.size_gb} GiB${d.volume_type ? ` type ${d.volume_type}` : ''}${d.multiattach ? ' multi-attach' : ''}${d.encrypted ? ' encrypted' : ''}${d.independent ? ' independent' : ''}`)
    .join('; ');
  const rate = vm.change_rate_bps != null ? `${(vm.change_rate_bps / MiB).toFixed(1)} MiB/s measured` : 'unknown (default 2 MiB/s assumed)';
  const estimateText = estimates
    .map((e) => `${e.strategy}: downtime ${Math.round(e.downtime_s)} s, pre-copy ${Math.round(e.precopy_s)} s over ${e.passes} pass(es), ${e.eligible ? 'eligible' : `INELIGIBLE (${e.reasons.join('; ')})`}`)
    .join('; ');
  const eligible = estimates.filter((e) => e.eligible);
  const payload = {
    tool: 'jev_decide',
    arguments: {
      decision: `Which migration strategy should be used for VM ${vm.name} moving to RHOSO 18.0?`,
      evidence:
        `VM ${vm.name}: ${vm.vcpus} vCPU, ${vm.ram_mb} MB RAM, ${vm.disks.length} disk(s) [${disks}], provisioned ${gib(vm.disk_bytes)}, used ${gib(vm.used_bytes)}. ` +
        `Guest write rate ${rate}. Downtime SLO ${plan.downtime_slo_s} s. Link ${Math.round(plan.link_bps / MiB)} MiB/s. ` +
        `Findings: ${findings.map((f) => `${f.code} (${f.severity})`).join(', ') || 'none'}. Estimates: ${estimateText}.`,
      priorities: `Minimize downtime within the SLO of ${plan.downtime_slo_s} s; prefer the simpler strategy when downtimes differ by less than 10 %; never pick an ineligible strategy`,
      candidates: eligible.map((e) => ({ id: e.strategy, description: CANDIDATE_DESCRIPTIONS[e.strategy] })),
      requirements: STRATEGY_REQUIREMENTS,
      escalate_on_contradiction: true,
    },
    scope: [...input.scope],
    options: eligible.map((e) => e.strategy),
    deterministic: input.deterministic,
    deterministic_reason: `${input.deterministic}: the deterministic choice of the selection policy`,
  };
  return { sha256: sha256Hex(canonicalJson(payload)), created_at: input.at, payload };
}

export function iso(ms: number): string {
  return new Date(Math.round(ms / 1000) * 1000).toISOString().replace('.000Z', 'Z');
}

/** Small deterministic PRNG (mulberry32). */
export function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// -------------------------------------------------------------------------------------------
// VMs
// -------------------------------------------------------------------------------------------

type DiskSpec = Partial<Disk> & { size_gb: number };
interface VmSpec {
  id: string;
  name: string;
  project?: string;
  flavor?: string;
  vcpus?: number;
  ram_mb?: number;
  disks: DiskSpec[];
  nics?: Partial<Nic>[];
  power_state?: VMRef['power_state'];
  os_type?: string;
  host?: string;
  tags?: Record<string, string>;
  flavor_extra_specs?: Record<string, string>;
  cbt_enabled?: boolean | null;
  snapshot_count?: number;
  tools_ok?: boolean | null;
  change_rate_mibps?: number;
}

/** Cinder pool of each mock volume type (SDD §4.2 `Disk.pool`): Ceph RBD and an ONTAP NFS export. */
const MOCK_POOLS: Record<string, string> = {
  'tripleo-ceph': 'overcloud@tripleo_ceph#tripleo-ceph',
  'tripleo-ceph-ssd': 'overcloud@tripleo_ceph#tripleo-ceph-ssd',
  'ceph-ssd': 'overcloud@tripleo_ceph#ceph-ssd',
  'ceph-hdd': 'overcloud@tripleo_ceph#ceph-hdd',
  'netapp-nfs': 'overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1',
  lvm: 'lab@lvm#lvm',
};

const backend = (pool: string, family: 'rbd' | 'netapp_nfs' | 'other') => ({
  pool,
  vendor: family === 'netapp_nfs' ? 'NetApp' : 'Open Source',
  protocol: family === 'netapp_nfs' ? 'nfs' : family === 'rbd' ? 'ceph' : 'iSCSI',
  family,
});

const DC1_STORAGE = [
  backend('overcloud@tripleo_ceph#ceph-hdd', 'rbd'),
  backend('overcloud@tripleo_ceph#ceph-ssd', 'rbd'),
  backend('overcloud@tripleo_ceph#tripleo-ceph', 'rbd'),
  backend('overcloud@tripleo_ceph#tripleo-ceph-ssd', 'rbd'),
  backend('overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1', 'netapp_nfs'),
];

const PROD_STORAGE = [
  backend('hostgroup@ceph-hdd#volumes-hdd', 'rbd'),
  backend('hostgroup@ceph-nvme#volumes-nvme', 'rbd'),
  backend('hostgroup@ceph-ssd#volumes-ssd', 'rbd'),
  // the same ONTAP export, mounted through the RHOSO storage network's LIF
  backend('hostgroup@ontap-nfs#198.51.100.60:/cinder_dc1', 'netapp_nfs'),
];

export function makeVm(spec: VmSpec): VMRef {
  const disks: Disk[] = spec.disks.map((d, i) => {
    const kind = d.kind ?? 'volume';
    const volumeType = d.volume_type === undefined ? 'tripleo-ceph' : d.volume_type;
    return {
    id: d.id ?? `${spec.id}-disk-${i}`,
    name: d.name ?? `${spec.name}-${i === 0 ? 'root' : `data${i}`}`,
    size_gb: d.size_gb,
    used_gb: d.used_gb === undefined ? null : d.used_gb,
    bootable: d.bootable ?? i === 0,
    volume_type: volumeType,
    device: d.device ?? `/dev/vd${String.fromCharCode(97 + i)}`,
    kind,
    multiattach: d.multiattach ?? false,
    encrypted: d.encrypted ?? false,
    independent: d.independent ?? false,
    pool: d.pool !== undefined ? d.pool : kind === 'volume' && volumeType ? (MOCK_POOLS[volumeType] ?? null) : null,
    };
  });
  const diskBytes = disks.reduce((sum, d) => sum + d.size_gb * GiB, 0);
  const everyUsed = disks.every((d) => d.used_gb !== null);
  const usedBytes = everyUsed
    ? Math.round(disks.reduce((sum, d) => sum + (d.used_gb ?? 0) * GiB, 0))
    : Math.floor(diskBytes * 0.6);
  return {
    source_id: spec.id,
    name: spec.name,
    project: spec.project ?? null,
    flavor: spec.flavor ?? null,
    vcpus: spec.vcpus ?? 2,
    ram_mb: spec.ram_mb ?? 4096,
    disks,
    nics: (spec.nics ?? [{ network: 'tenant-app' }]).map((n, i) => ({
      network: n.network ?? 'tenant-app',
      mac: n.mac ?? `fa:16:3e:${(i + 10).toString(16).padStart(2, '0')}:${spec.id.slice(-4, -2)}:${spec.id.slice(-2)}`,
      fixed_ips: n.fixed_ips ?? [`10.20.${i + 1}.${(parseInt(spec.id.slice(-2), 16) % 200) + 10}`],
      vnic_type: n.vnic_type ?? 'normal',
      mtu: n.mtu === undefined ? 1450 : n.mtu,
    })),
    power_state: spec.power_state ?? 'running',
    os_type: spec.os_type ?? 'rhel9',
    host: spec.host ?? null,
    tags: spec.tags ?? {},
    flavor_extra_specs: spec.flavor_extra_specs ?? {},
    cbt_enabled: spec.cbt_enabled === undefined ? null : spec.cbt_enabled,
    snapshot_count: spec.snapshot_count ?? 0,
    tools_ok: spec.tools_ok === undefined ? null : spec.tools_ok,
    change_rate_bps: spec.change_rate_mibps === undefined ? null : spec.change_rate_mibps * MiB,
    disk_bytes: diskBytes,
    used_bytes: usedBytes,
    guest_os: identifyGuestOs(spec.os_type ?? 'rhel9'),
  };
}

const OS_VMS: VmSpec[] = [
  { id: 'os-0a11', name: 'web-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 12 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-01' },
  { id: 'os-0a12', name: 'web-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'ubuntu 22.04', disks: [{ size_gb: 40, used_gb: 14 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-02' },
  { id: 'os-0a13', name: 'web-03', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 13 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'web' }, host: 'compute-03' },
  { id: 'os-0a14', name: 'web-cache-01', project: 'shop', flavor: 'm1.small', disks: [{ size_gb: 30, used_gb: 9 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'cache' } },
  { id: 'os-0b21', name: 'api-gw-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'rocky 9.4', disks: [{ size_gb: 60, used_gb: 21 }], tags: { app: 'gateway' } },
  { id: 'os-0b22', name: 'api-gw-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'debian 12', disks: [{ size_gb: 60, used_gb: 20 }], tags: { app: 'gateway' } },
  { id: 'os-0b31', name: 'mq-broker-01', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 46, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0b32', name: 'mq-broker-02', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 51, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0c41', name: 'ad-dc-01', project: 'platform', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', disks: [{ size_gb: 80, used_gb: 38 }], nics: [{ network: 'tenant-infra', mtu: 1500 }], tags: { app: 'active-directory' } },
  { id: 'os-0d51', name: 'db-pg-01', project: 'finance', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 18 }, { size_gb: 600, used_gb: 410, volume_type: 'tripleo-ceph-ssd', encrypted: true }], nics: [{ network: 'tenant-db' }], tags: { app: 'ledger', tier: 'database' }, change_rate_mibps: 8 },
  { id: 'os-0d52', name: 'db-mysql-02', project: 'shop', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 16 }, { size_gb: 520, used_gb: 300, volume_type: 'tripleo-ceph-ssd' }], nics: [{ network: 'tenant-db' }], tags: { app: 'shop', tier: 'database' }, change_rate_mibps: 5 },
  { id: 'os-0d53', name: 'db-oracle-03', project: 'finance', flavor: 'db.xlarge', vcpus: 16, ram_mb: 131072, disks: [{ size_gb: 100, used_gb: 40 }, { size_gb: 1200, used_gb: 900, volume_type: 'tripleo-ceph-ssd' }], nics: [{ network: 'tenant-db' }], tags: { app: 'erp-finance', tier: 'database' }, change_rate_mibps: 12 },
  { id: 'os-0e61', name: 'legacy-rhel6-app', project: 'finance', flavor: 'm1.medium', os_type: 'rhel6', disks: [{ size_gb: 80, used_gb: 33 }], tags: { app: 'payroll' } },
  { id: 'os-0e62', name: 'gpu-render-01', project: 'media', flavor: 'g1.a10', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 200, used_gb: 120 }], flavor_extra_specs: { 'resources:VGPU': '1' }, tags: { app: 'render' } },
  { id: 'os-0e63', name: 'shared-disk-node-a', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 12 }, { size_gb: 300, used_gb: 140, multiattach: true }], tags: { app: 'gfs-cluster' } },
  { id: 'os-0e64', name: 'app-billing-01', project: 'finance', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 50, used_gb: 22 }, { size_gb: 150, used_gb: 88 }], tags: { app: 'billing' }, change_rate_mibps: 3 },
  { id: 'os-0e65', name: 'static-cdn-01', project: 'shop', flavor: 'm1.small', disks: [{ size_gb: 20, used_gb: 6, kind: 'image_root', volume_type: null }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'static' } },
  { id: 'os-0e66', name: 'report-gen-01', project: 'finance', flavor: 'm1.medium', os_type: 'windows 2022', disks: [{ size_gb: 60, used_gb: 25, volume_type: 'netapp-nfs' }], tags: { app: 'reporting' } },
  { id: 'os-0e67', name: 'old-ftp-01', project: 'platform', flavor: 'm1.small', power_state: 'stopped', disks: [{ size_gb: 30, used_gb: 4, volume_type: 'netapp-nfs' }], tags: { app: 'ftp' } },
  { id: 'os-0f71', name: 'analytics-node-1', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 14, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 380, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f72', name: 'analytics-node-2', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 14, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 362, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f73', name: 'analytics-node-3', project: 'analytics', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 40, used_gb: 15, volume_type: 'ceph-ssd' }, { size_gb: 500, used_gb: 371, volume_type: 'ceph-ssd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark' } },
  { id: 'os-0f74', name: 'analytics-db', project: 'analytics', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 17, volume_type: 'ceph-ssd' }, { size_gb: 400, used_gb: 260, volume_type: 'ceph-hdd' }], nics: [{ network: 'analytics' }], tags: { app: 'spark', tier: 'database' }, change_rate_mibps: 6 },
  { id: 'os-1a01', name: 'jump-host-01', project: 'platform', flavor: 'm1.small', os_type: 'ubuntu 18.04', disks: [{ size_gb: 20, used_gb: 5 }], nics: [{ network: 'provider-ext', mtu: 1500, fixed_ips: ['203.0.113.25'] }], tags: { app: 'bastion' } },
  { id: 'os-1a02', name: 'build-runner-07', project: 'platform', flavor: 'm1.large', power_state: 'error', disks: [{ size_gb: 100 }], tags: { app: 'ci' } },
];

const VMW_VMS: VmSpec[] = [
  { id: 'vm-5001', name: 'vm-erp-app-01', project: 'erp', vcpus: 8, ram_mb: 32768, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 120, used_gb: 64, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp' }, change_rate_mibps: 4 },
  { id: 'vm-5002', name: 'vm-erp-db-01', project: 'erp', vcpus: 16, ram_mb: 131072, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 100, used_gb: 30, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 800, used_gb: 590, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp', tier: 'database' }, change_rate_mibps: 10 },
  { id: 'vm-5003', name: 'vm-fileserver-01', project: 'erp', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', cbt_enabled: false, tools_ok: true, disks: [{ size_gb: 100, used_gb: 40, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 1500, used_gb: 1100, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'files' } },
  { id: 'vm-5004', name: 'vm-print-01', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2016', cbt_enabled: true, tools_ok: false, disks: [{ size_gb: 60, used_gb: 22, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 20, used_gb: 3, kind: 'vmdk', volume_type: null, device: 'scsi0:1', independent: true }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'print' } },
  { id: 'vm-5005', name: 'vm-hr-portal', project: 'erp', vcpus: 4, ram_mb: 8192, os_type: 'Ubuntu 24.04.1 LTS', cbt_enabled: true, tools_ok: true, snapshot_count: 2, disks: [{ size_gb: 80, used_gb: 31, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'hr' } },
  { id: 'vm-5006', name: 'vm-legacy-win2008', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2008', cbt_enabled: false, tools_ok: false, disks: [{ size_gb: 60, used_gb: 41, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'legacy-crm' } },
];

const LAB_VMS: VmSpec[] = [
  { id: 'lab-2b01', name: 'lab-k8s-master', project: 'lab', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, disks: [{ size_gb: 80, used_gb: 25, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
  { id: 'lab-2b02', name: 'lab-k8s-worker-1', project: 'lab', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 120, used_gb: 48, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
];

// -------------------------------------------------------------------------------------------
// Providers
// -------------------------------------------------------------------------------------------

function conversionHost(name: string, address: string) {
  return { manage: true, name, flavor: 'm1.large', external_network: 'provider-ext', image: 'rhel-9.4-conversion', ssh_user: 'cloud-user', address, ssh_allowed_cidr: null, ssh_key_secret: null };
}

export function buildProviders(now: number): Provider[] {
  return [
    {
      id: 'rhosp17-dc1',
      name: 'RHOSP 17.1 — DC1',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://overcloud.dc1.example.com:13000/v3',
      cloud: null,
      credentials_secret: 'provider-rhosp17-dc1',
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/dc1-ca.pem',
      conversion_host: conversionHost('seamless-conv-src', '192.0.2.21'),
      capabilities: { admin: true, roles: ['admin', 'member', 'reader'], compute_microversion: '2.79', ovn: false, volume_backends: DC1_STORAGE.map((b) => b.pool), storage_backends: DC1_STORAGE },
      status: 'ok',
      status_message: 'Keystone, Nova, Cinder, Neutron and Glance reachable.',
      last_checked_at: iso(now - 4 * 60_000),
      distribution: 'rhosp',
      credentials_updated_at: iso(now - 3 * 3600_000),
      conversion_key_updated_at: null,
    },
    {
      id: 'vcenter-hq',
      name: 'vCenter HQ',
      kind: 'vmware',
      role: 'source',
      endpoint: 'https://vcenter.hq.example.com/sdk',
      cloud: null,
      credentials_secret: 'provider-vcenter-hq',
      region: 'Datacenter-HQ',
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: { manage: false, name: 'conv-vddk-hq', flavor: null, external_network: null, image: null, ssh_user: 'cloud-user', address: '198.51.100.40', ssh_allowed_cidr: null, ssh_key_secret: 'provider-vcenter-hq-ssh' },
      capabilities: { version: '8.0.2', cbt: true, datastores: ['vsanDatastore', 'nfs-archive'] },
      status: 'degraded',
      status_message: 'Two ESXi hosts in maintenance mode; CBT queries are slow.',
      last_checked_at: iso(now - 11 * 60_000),
      distribution: 'vmware',
      credentials_updated_at: iso(now - 26 * 3600_000),
      conversion_key_updated_at: iso(now - 26 * 3600_000),
    },
    {
      id: 'community-lab',
      name: 'OpenStack Lab (2023.1)',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://lab-openstack.example.com:5000/v3',
      cloud: 'community-lab',
      credentials_secret: null,
      region: 'RegionOne',
      verify_tls: false,
      ca_cert_path: null,
      conversion_host: null,
      capabilities: {},
      status: 'error',
      status_message: 'Keystone returned HTTP 503 Service Unavailable.',
      last_checked_at: iso(now - 26 * 60_000),
      distribution: 'openstack_community',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'kolla-edge',
      name: 'Kolla Edge (Bobcat)',
      kind: 'openstack',
      role: 'source',
      endpoint: 'https://kolla-vip.edge.example.com:5000/v3',
      cloud: null,
      credentials_secret: null,
      region: 'RegionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/kolla-root.crt',
      conversion_host: null,
      capabilities: {},
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      distribution: 'kolla',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'rhoso-prod',
      name: 'RHOSO 18.0 — Production',
      kind: 'rhoso',
      role: 'destination',
      endpoint: 'https://keystone-public-openstack.apps.ocp.example.com/v3',
      cloud: 'rhoso-prod',
      credentials_secret: null,
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: '/etc/pki/seamless/ocp-ingress-ca.pem',
      conversion_host: conversionHost('seamless-conv-dst', '198.51.100.31'),
      capabilities: {
        admin: true,
        // an admin token (SDD §10): jump-host-01 keeps its address on the shared provider-ext (§6.6)
        roles: ['admin', 'member', 'reader'],
        compute_microversion: '2.95',
        ovn: true,
        volume_backends: PROD_STORAGE.map((b) => b.pool),
        storage_backends: PROD_STORAGE,
      },
      status: 'ok',
      status_message: 'All services healthy; OVN networking.',
      last_checked_at: iso(now - 3 * 60_000),
      distribution: 'rhoso',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      id: 'rhoso-staging',
      name: 'RHOSO 18.0 — Staging',
      kind: 'rhoso',
      role: 'destination',
      endpoint: 'https://keystone-public-openstack.apps.ocp-stg.example.com/v3',
      cloud: 'rhoso-staging',
      credentials_secret: null,
      region: 'regionOne',
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: null,
      capabilities: {},
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      distribution: 'rhoso',
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
  ];
}

export function buildInventories(): Record<string, VMRef[] | DestinationInventory> {
  const destination: DestinationInventory = {
    networks: { 'tenant-web': 1442, 'tenant-app': 1442, 'tenant-db': 1442, 'tenant-infra': 1442, analytics: 1442, 'erp-net': 1442, 'provider-ext': 1500 },
    flavors: [
      { name: 'm1.small', vcpus: 2, ram_mb: 4096, disk_gb: 40, extra_specs: {} },
      { name: 'm1.medium', vcpus: 4, ram_mb: 8192, disk_gb: 80, extra_specs: {} },
      { name: 'm1.large', vcpus: 8, ram_mb: 32768, disk_gb: 160, extra_specs: {} },
      { name: 'db.xlarge', vcpus: 16, ram_mb: 131072, disk_gb: 200, extra_specs: { 'hw:cpu_policy': 'dedicated' } },
    ],
    volume_types: ['ceph-ssd', 'ceph-hdd', 'ceph-nvme', 'netapp-nfs'],
    quotas: {
      shop: { cores: 120, ram_mb: 262144, instances: 40, volumes: 80, gigabytes: 8000 },
      finance: { cores: 96, ram_mb: 393216, instances: 20, volumes: 40, gigabytes: 6000 },
      platform: { cores: 64, ram_mb: 131072, instances: 30, volumes: 60, gigabytes: 4000 },
      analytics: { cores: 128, ram_mb: 524288, instances: 16, volumes: 32, gigabytes: 12000 },
      erp: { cores: 64, ram_mb: 262144, instances: 20, volumes: 40, gigabytes: 9000 },
    },
    projects: ['shop', 'finance', 'platform', 'analytics', 'erp', 'media'],
    // where a NIC's addresses must lie (SDD §11): the mock VMs' addresses are in 10.20.0.0/16
    subnets: [
      ...['tenant-web', 'tenant-app', 'tenant-db', 'tenant-infra', 'analytics', 'erp-net'].map((network) => ({
        name: `${network}-v4`,
        network,
        network_id: `net-${network}`,
        cidr: '10.20.0.0/16',
        ipv6_address_mode: null,
        ipv6_ra_mode: null,
      })),
      { name: 'provider-ext-v4', network: 'provider-ext', network_id: 'net-provider-ext', cidr: '203.0.113.0/24', ipv6_address_mode: null, ipv6_ra_mode: null },
    ],
  };
  // what a NIC's network name resolves to (SDD §11): the credential's own networks, and a provider network another project shares
  destination.network_details = Object.entries(destination.networks).map(([name, mtu]) => ({
    id: `net-${name}`,
    name,
    mtu,
    project_id: name === 'provider-ext' ? 'admin' : 'migration',
    own: name !== 'provider-ext',
    shared: name === 'provider-ext',
  }));
  return {
    'rhosp17-dc1': OS_VMS.map(makeVm),
    'vcenter-hq': VMW_VMS.map(makeVm),
    'community-lab': LAB_VMS.map(makeVm),
    'rhoso-prod': destination,
    'rhoso-staging': { networks: {}, flavors: [], volume_types: [], quotas: {}, projects: [] },
  };
}

// -------------------------------------------------------------------------------------------
// Estimator (SDD §9.1) and eligibility (§9.2)
// -------------------------------------------------------------------------------------------

const PARAMS = {
  scan_bps: 524288000,
  change_rate_bps: 2097152,
  shutdown_s: 60,
  boot_s: 120,
  create_s: 60,
  snapshot_s: 30,
  handover_per_volume_s: 20,
  v2v_s: 300,
  v2v_inplace_s: 120,
};

export function estimate(vm: VMRef, strategy: Strategy, plan: Plan, reasons: string[]): Estimate {
  const D = vm.disk_bytes;
  const U = vm.used_bytes;
  const c = vm.change_rate_bps ?? PARAMS.change_rate_bps;
  const L = plan.link_bps;
  const S = PARAMS.scan_bps;
  const V = vm.disks.length;
  const threshold = plan.convergence_threshold_bytes;
  const maxPasses = plan.max_sync_passes;
  let precopy = 0;
  let passes = 0;
  let finalDelta = 0;

  // the copy, or final sync, and the conversion: the parts of the downtime besides the fixed steps (SDD §9.1)
  let copy = 0;
  let conversion = 0;
  switch (strategy) {
    case 'cold':
      copy = PARAMS.snapshot_s + U / L;
      break;
    case 'warm': {
      let t = PARAMS.snapshot_s + Math.max(U / L, D / S);
      precopy = t;
      passes = 1;
      let delta = Math.min(D, c * t);
      while (delta > threshold && passes < maxPasses) {
        t = PARAMS.snapshot_s + Math.max(D / S, delta / L);
        precopy += t;
        passes += 1;
        delta = Math.min(D, c * t);
      }
      finalDelta = delta;
      copy = PARAMS.snapshot_s + Math.max(D / S, finalDelta / L);
      break;
    }
    case 'storage_handover':
      copy = V * PARAMS.handover_per_volume_s;
      break;
    case 'vmware_cold':
      copy = U / L;
      conversion = PARAMS.v2v_s;
      break;
    case 'vmware_warm': {
      let t = U / L;
      precopy = t;
      passes = 1;
      let delta = Math.min(D, c * t);
      while (delta > threshold && passes < maxPasses) {
        t = 10 + delta / L;
        precopy += t;
        passes += 1;
        delta = Math.min(D, c * t);
      }
      finalDelta = delta;
      copy = finalDelta / L;
      conversion = PARAMS.v2v_inplace_s;
      break;
    }
  }
  const round = (n: number) => Math.round(n * 10) / 10;
  // the API's keys (SDD §4, ADV-16): the rounded parts add up to the downtime
  const downtime_parts = {
    shutdown_s: PARAMS.shutdown_s,
    copy_s: round(copy),
    conversion_s: round(conversion),
    create_s: PARAMS.create_s,
    boot_s: PARAMS.boot_s,
  };
  const downtime = round(Object.values(downtime_parts).reduce((a, b) => a + b, 0));
  return {
    strategy,
    eligible: reasons.length === 0,
    reasons,
    precopy_s: round(precopy),
    passes,
    downtime_s: downtime,
    total_s: round(precopy + downtime),
    final_delta_bytes: Math.round(finalDelta),
    meets_slo: downtime <= plan.downtime_slo_s,
    downtime_parts,
  };
}

/** Per-volume driver-family checks of a handover where the pools are known (SDD §9.2, §7.3.1). */
function storageReasons(vm: VMRef, plan: Plan, source: Provider, destination: Provider): string[] {
  const families = new Map(storageBackends(source.capabilities).map((b) => [b.pool, b.family]));
  const dst = storageBackends(destination.capabilities);
  const reasons: string[] = [];
  for (const disk of vm.disks) {
    if (disk.kind !== 'volume' || !disk.pool || !families.has(disk.pool)) continue;
    const family = families.get(disk.pool)!;
    const target = plan.handover.backend_map[disk.volume_type ?? ''];
    if (family !== 'other' && (!target || dst.length === 0)) continue;
    const { error } = resolveDestination(family, splitHost(disk.pool)[1], target ?? '', dst);
    if (error) reasons.push(`${disk.name ?? disk.id}: ${error}`);
  }
  return reasons;
}


/** The VM with each NIC's addresses as the plan gives them (SDD §9.2): its override's, else its own. */
function withPlannedAddresses(plan: Plan, vm: VMRef): VMRef {
  return { ...vm, nics: vm.nics.map((nic) => ({ ...nic, fixed_ips: nicTarget(plan, vm.source_id, nic).addresses })) };
}

export function eligibility(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  source: Provider,
  destination: Provider,
  findings: Finding[],
): Partial<Record<Strategy, string[]>> {
  const result: Partial<Record<Strategy, string[]>> = {};
  const blockers = findings.filter((f) => f.severity === 'blocker').map((f) => `Blocker finding ${f.code}`);
  const multiattach = vm.disks.some((d) => d.multiattach);
  if (sourceKind === 'vmware') {
    result.vmware_cold = vm.power_state === 'error' ? ['Source VM is in error state'] : [];
    const warm: string[] = [];
    if (vm.cbt_enabled !== true) warm.push('Changed Block Tracking is not enabled');
    if (vm.disks.some((d) => d.independent)) warm.push('VM has an independent disk');
    result.vmware_warm = warm;
  } else {
    const cold: string[] = [];
    if (vm.power_state === 'error') cold.push('Source VM is in error state');
    if (!source.conversion_host || !destination.conversion_host) cold.push('No conversion host configured on both clouds');
    // through Nova os-migrate recreates each port with exactly one fixed IP; through Neutron every one (SDD §9.2)
    const planned = withPlannedAddresses(plan, vm);
    const oddPorts = portsWithoutOneFixedIp(planned);
    if (oddPorts.length && plan.port_creation_mode !== 'neutron') cold.push(`Port(s) without exactly one fixed IP, which Nova cannot recreate: ${describePorts(oddPorts)}`);
    result.cold = cold;
    result.warm = [...cold, ...(multiattach ? ['VM has a multi-attach volume'] : [])];
    const handover: string[] = [];
    if (!plan.handover.enabled) handover.push('Storage handover is not enabled for this plan');
    if (vm.disks.some((d) => d.kind !== 'volume')) handover.push('Not every disk is a Cinder volume');
    const unmapped = vm.disks.filter((d) => !d.volume_type || !(d.volume_type in plan.handover.backend_map));
    if (plan.handover.enabled && unmapped.length) handover.push('A volume type has no RHOSO backend mapping');
    if (multiattach) handover.push('VM has a multi-attach volume');
    // through Nova the destination keeps one address per port; through Neutron every one (SDD §7.3 step 8)
    const several = planned.nics.filter((n) => n.fixed_ips.length > 1);
    if (several.length && plan.port_creation_mode !== 'neutron') handover.push(`Port(s) with several fixed IPs, of which one would be kept: ${describePorts(several)}`);
    if (vm.disks.some((d) => d.encrypted)) handover.push('Encrypted volumes cannot be unmanaged by Cinder');
    handover.push(...storageReasons(vm, plan, source, destination));
    if (!source.capabilities.admin || !destination.capabilities.admin) handover.push('Admin access is required on both clouds');
    result.storage_handover = handover;
  }
  if (blockers.length) {
    for (const key of Object.keys(result) as Strategy[]) result[key] = [...(result[key] ?? []), ...blockers];
  }
  return result;
}

// -------------------------------------------------------------------------------------------
// Pre-flight findings (subset of SDD §9.3)
// -------------------------------------------------------------------------------------------

const NIC_OVERRIDE_REMEDIATION: Record<string, string> = {
  MAP_NIC_OVERRIDE_INVALID:
    "Fix the VM's network overrides on its migration page: an existing RHOSO network, addresses inside its subnets, each given once in the plan. VMware VMs take none.",
  MAP_NIC_OVERRIDE: 'Nothing to do: each NIC goes where its override says.',
};

/** The API's catalog remediations of the network checks (SDD §9.3). */
const NETWORK_REMEDIATION = {
  MAP_NETWORK_AMBIGUOUS:
    "Send the NIC to a name one RHOSO network carries: one of the credential's project, or the only one of that name - map its network to such a name, or rename one of the networks.",
  NET_ADDRESS_REFUSED:
    'Give the NIC an address its RHOSO network takes: map its network to one whose subnets hold it, or set the address in a NIC override; for a SLAAC address keep the MAC (port_creation_mode "neutron").',
  NET_PORT_NOT_PERMITTED:
    "Send the NIC to a network of the credential's project: map its network to one, or set a NIC override on the VM's migration page. Another project's shared or external network takes it once the RHOSO credential has the admin role.",
};

/** Like the API (SDD §9.3): every entry of the VM checked before anything stops, with the API's messages. */
function nicOverrideFindings(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  destinationNetworks: Record<string, number | null>,
  context: { subnets?: DestinationSubnet[]; selected?: VMRef[]; networkDetails?: DestinationNetwork[] },
): Array<['MAP_NIC_OVERRIDE' | 'MAP_NIC_OVERRIDE_INVALID', string]> {
  const entries = plan.nic_overrides?.[vm.source_id] ?? {};
  const macs = Object.keys(entries).sort();
  if (!macs.length) return [];
  if (sourceKind === 'vmware')
    return [['MAP_NIC_OVERRIDE_INVALID', `${vm.name} is a VMware VM: the migration kit keeps the guest's network settings, so its NIC overrides cannot be applied (${macs.join(', ')}).`]];
  const holders = new Map<string, Array<{ vmId: string; mac: string; name: string }>>();
  for (const other of context.selected ?? [vm]) {
    for (const n of other.nics) {
      const target = nicTarget(plan, other.source_id, n);
      for (const a of target.addresses) {
        const key = `${target.network}|${canonicalIp(a)}`;
        holders.set(key, [...(holders.get(key) ?? []), { vmId: other.source_id, mac: (n.mac ?? '').toLowerCase(), name: other.name }]);
      }
    }
  }
  const problems: string[] = [];
  const applied: string[] = [];
  for (const mac of macs) {
    const nic = vm.nics.find((n) => n.mac?.toLowerCase() === mac);
    if (!nic) {
      problems.push(`${mac}: the VM has no NIC with this MAC`);
      continue;
    }
    const { network, addresses } = nicTarget(plan, vm.source_id, nic);
    if (!(network in destinationNetworks)) {
      problems.push(
        `${mac}: RHOSO has no network '${network}'` +
          (entries[mac]?.network ? '' : ' yet: pre-staging would create it, but its subnets cannot be checked before; name an existing RHOSO network'),
      );
      continue;
    }
    // the network the cutover resolves the name to; MAP_NETWORK_AMBIGUOUS names one it cannot
    const resolved = resolveNetwork(networkList({ networks: destinationNetworks, network_details: context.networkDetails }), network).network;
    if (!resolved) continue;
    const subnets = subnetsOf(context.subnets ?? [], resolved);
    const cidrs = subnets.map((s) => s.cidr);
    const outside = addresses.filter((a) => !cidrs.some((c) => ipInCidr(a, c)));
    if (outside.length) problems.push(`${mac}: ${outside.join(', ')} ${outside.length === 1 ? 'lies' : 'lie'} in no subnet of '${network}'`);
    const neutron = plan.port_creation_mode === 'neutron';
    for (const a of addresses) {
      const reserved = noHostAddress(a, cidrs);
      if (reserved) problems.push(`${mac}: ${a} is ${reserved}, not a host address`);
      else if (!outside.includes(a)) {
        // on a SLAAC or DHCPv6-stateless subnet only what Neutron derives (SDD §6.6)
        const why = addressRefusal(a, subnets, { neutron, mac });
        if (why) problems.push(`${mac}: ${a} ${why}`);
      }
    }
    for (const a of addresses)
      for (const h of holders.get(`${network}|${canonicalIp(a)}`) ?? []) if (h.vmId !== vm.source_id || h.mac !== mac) problems.push(`${mac}: ${a} also goes to ${h.name} (${h.mac})`);
    applied.push(`${mac} (${nic.network}) -> ${network}: ${addresses.join(', ')}`);
  }
  if (problems.length) return [['MAP_NIC_OVERRIDE_INVALID', `NIC override(s) of ${vm.name} cannot be applied: ${problems.join('; ')}.`]];
  if (!applied.length) return [];
  return [['MAP_NIC_OVERRIDE', `NIC override(s): ${applied.join('; ')}.`]];
}

export function preflight(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  destinationNetworks: Record<string, number | null>,
  selectedNames: string[],
  /** The destination's subnets, its networks with their owners, its token's roles and the plan's VMs, for the NIC checks (SDD §9.3). */
  context: { subnets?: DestinationSubnet[]; selected?: VMRef[]; networkDetails?: DestinationNetwork[]; roles?: string[] } = {},
): Finding[] {
  const findings: Finding[] = [];
  const add = (code: string, severity: Finding['severity'], message: string, remediation: string | null, strategies: Strategy[] = []) =>
    findings.push({ code, severity, message, remediation, strategies });

  if (vm.power_state === 'error') add('SRC_VM_ERROR_STATE', 'blocker', `${vm.name} is in ERROR state at the source.`, 'Repair or reset the source instance, then re-validate.');
  if (vm.power_state === 'transitioning') add('SRC_VM_TRANSITIONAL_STATE', 'blocker', `${vm.name} has a Nova task in flight (resize, migration, rescue, rebuild or reboot).`, 'Wait until the VM is ACTIVE or SHUTOFF, then re-validate.');
  if (selectedNames.filter((n) => n === vm.name).length > 1)
    add('SRC_VM_DUPLICATE_NAME', 'blocker', `Another selected VM is also named ${vm.name}; os-migrate filters workloads by name.`, 'Rename one of the instances or split them into separate plans.');
  if (vm.disks.some((d) => d.multiattach))
    add('SRC_VM_MULTIATTACH', 'warning', 'A multi-attach volume is attached; warm and storage handover are not possible.', 'Detach the shared volume or migrate cold during a window.', ['warm', 'storage_handover']);
  if (vm.disks[0] && (vm.disks[0].kind === 'image_root' || vm.disks[0].kind === 'ephemeral'))
    add('SRC_VM_EPHEMERAL_ROOT', 'info', 'Root disk is image-backed; each warm pass snapshots the server to Glance.', 'Expect slower pre-copy passes (boot_disk_copy).', ['warm']);
  // every NIC's RHOSO network as the cutover resolves its name (SDD §7.3 step 0, §9.3); the VMware kit maps port groups its own way
  const seen = networkList({ networks: destinationNetworks, network_details: context.networkDetails });
  const resolved = new Map<Nic, DestinationNetwork>();
  const ambiguous: string[] = [];
  for (const nic of vm.nics) {
    const { network, why } = resolveNetwork(seen, nicTarget(plan, vm.source_id, nic).network);
    if (network) resolved.set(nic, network);
    else if (why && sourceKind !== 'vmware') ambiguous.push(`${(nic.mac ?? '?').toLowerCase()} (${nic.network}): ${why}`);
  }
  if (ambiguous.length)
    add('MAP_NETWORK_AMBIGUOUS', 'blocker', `The cutover cannot tell these RHOSO networks apart: ${ambiguous.join('; ')}.`, NETWORK_REMEDIATION.MAP_NETWORK_AMBIGUOUS);
  // whether the credential may put each NIC's port on its resolved network, as the API (SDD §6.6, §9.3)
  const denied: string[] = [];
  for (const nic of sourceKind === 'vmware' ? [] : vm.nics) {
    const network = resolved.get(nic);
    const why = network ? portRefusal(network, context.roles) : null;
    if (network && why) denied.push(`${(nic.mac ?? '?').toLowerCase()} on '${network.name}' (project ${network.project_id}): ${why}`);
  }
  if (denied.length)
    add('NET_PORT_NOT_PERMITTED', 'blocker', `The RHOSO credential may not put these NICs on their network: ${denied.join('; ')}.`, NETWORK_REMEDIATION.NET_PORT_NOT_PERMITTED);
  for (const nic of vm.nics) {
    const mapped = plan.mappings.networks[nic.network] ?? nic.network;
    // a NIC its override sends elsewhere is checked by MAP_NIC_OVERRIDE_INVALID (SDD §9.3)
    if (sourceKind !== 'vmware' && !nicOverride(plan, vm.source_id, nic.mac)?.network && !(mapped in destinationNetworks))
      add('MAP_NETWORK_MISSING', 'blocker', `Network ${nic.network} has no mapping and no same-named RHOSO network.`, 'Add a network mapping in the plan.');
    const dstMtu = resolved.has(nic) ? resolved.get(nic)!.mtu : destinationNetworks[nicTarget(plan, vm.source_id, nic).network];
    if (nic.mtu && dstMtu && dstMtu < nic.mtu)
      add('NET_MTU_SHRINK', 'warning', `Destination MTU ${dstMtu} is smaller than the source NIC MTU ${nic.mtu} on ${nic.network}.`, 'Lower the guest MTU or enable jumbo frames on the OVN underlay.');
    if (['direct', 'direct-physical', 'macvtap'].includes(nic.vnic_type))
      add('NET_SRIOV_PORT', 'warning', `Port on ${nic.network} uses vnic_type ${nic.vnic_type}.`, 'Re-create SR-IOV ports on RHOSO manually.');
  }
  const oddPorts = portsWithoutOneFixedIp(withPlannedAddresses(plan, vm));
  if (sourceKind !== 'vmware' && oddPorts.length && plan.port_creation_mode !== 'neutron')
    add(
      'NET_PORT_IPS',
      'warning',
      `NIC(s) without exactly one fixed IP: ${describePorts(oddPorts)}. Cold and warm migration cannot recreate them; storage handover keeps one address per port.`,
      'Turn on "Keep MAC addresses and every fixed IP" in the plan (port_creation_mode "neutron"), give each such port exactly one fixed IP before the cutover, or migrate the VM by storage handover when its ports have at most one address each.',
    );
  // a NIC without an override keeps its own addresses: Neutron must take them on its resolved network (SDD §9.3, §6.6)
  if (sourceKind !== 'vmware' && context.subnets?.length) {
    const neutron = plan.port_creation_mode === 'neutron';
    const refused: string[] = [];
    for (const nic of vm.nics) {
      const network = resolved.get(nic);
      if (!network || nicOverride(plan, vm.source_id, nic.mac)) continue;
      const subnets = subnetsOf(context.subnets, network);
      for (const address of neutron ? nic.fixed_ips : nic.fixed_ips.slice(0, 1)) {
        const why = addressRefusal(address, subnets, { neutron, mac: nic.mac });
        if (why) refused.push(`${(nic.mac ?? '?').toLowerCase()} on '${network.name}': ${address} ${why}`);
      }
    }
    if (refused.length)
      add('NET_ADDRESS_REFUSED', 'blocker', `Neutron would refuse these addresses on their RHOSO network: ${refused.join('; ')}.`, NETWORK_REMEDIATION.NET_ADDRESS_REFUSED);
  }
  for (const [code, message] of nicOverrideFindings(vm, sourceKind, plan, destinationNetworks, context))
    add(code, code === 'MAP_NIC_OVERRIDE' ? 'info' : 'blocker', message, NIC_OVERRIDE_REMEDIATION[code] ?? null);
  if ('pci_passthrough:alias' in vm.flavor_extra_specs)
    add('VM_PCI_PASSTHROUGH', 'blocker', 'Flavor requests PCI passthrough.', 'Provide an equivalent PCI alias on RHOSO compute nodes.');
  if ('resources:VGPU' in vm.flavor_extra_specs)
    add('VM_VGPU', 'blocker', 'Flavor requests a vGPU (resources:VGPU).', 'Configure mediated devices on RHOSO and map the flavor.');
  if (vm.disks.some((d) => d.encrypted))
    add('VOL_ENCRYPTED', 'warning', 'An encrypted volume is attached; its Barbican key must be re-created at RHOSO.', 'Export the secret and register it in the RHOSO Key Manager before cutover.');
  const guest = guestOsOf(vm);
  if (guest.lifecycle === 'legacy')
    add('GUEST_OS_LEGACY', 'warning', `Guest OS ${guest.label} is out of standard vendor support.`, 'It still migrates: test the application on RHOSO, check the virtio drivers and plan a longer verification.');
  if (guest.family === 'unknown')
    add('GUEST_OS_UNKNOWN', 'info', `The guest OS is not identified${vm.os_type ? ` (${vm.os_type})` : ''}.`, 'Set the os_distro and os_version image properties or run VMware Tools; verification uses the Linux profile meanwhile.');
  if (sourceKind === 'vmware' && (guest.v2v === 'tech_preview' || guest.v2v === 'unverified'))
    add('GUEST_CONVERSION_UNVERIFIED', 'warning', `Converting ${guest.label} with virt-v2v is ${guest.v2v === 'tech_preview' ? 'a Technology Preview' : 'not supported by Red Hat'}.`, 'Run a test conversion of a copy first.', ['vmware_cold', 'vmware_warm']);
  if (sourceKind === 'vmware' && guest.v2v === 'unsupported')
    add('GUEST_CONVERSION_UNSUPPORTED', 'warning', `virt-v2v on the RHEL 9 conversion host cannot prepare ${guest.label}.`, 'Install the virtio storage and network drivers from an older virtio-win release inside the guest and test the conversion, or migrate the VM another way.', ['vmware_cold', 'vmware_warm']);
  if (sourceKind === 'vmware') {
    if (vm.cbt_enabled === false)
      add('VMW_CBT_DISABLED', 'warning', 'Changed Block Tracking is disabled; only cold migration is possible.', 'Enable CBT (requires a power cycle) or accept a cold window.', ['vmware_warm']);
    if (vm.disks.some((d) => d.independent))
      add('VMW_INDEPENDENT_DISK', 'warning', 'An independent disk cannot be tracked by CBT.', 'Convert the disk to dependent mode or migrate cold.', ['vmware_warm']);
    if (vm.snapshot_count > 0)
      add('VMW_SNAPSHOTS_PRESENT', 'warning', `${vm.snapshot_count} VMware snapshots exist.`, 'Consolidate snapshots before migration.');
    if (vm.tools_ok === false)
      add('VMW_TOOLS_MISSING', 'info', 'VMware Tools are not running; graceful shutdown may time out.', 'Install or start VMware Tools.');
  }
  if (plan.handover.enabled && sourceKind !== 'vmware' && vm.disks.some((d) => !d.volume_type || !(d.volume_type in plan.handover.backend_map)))
    add('HANDOVER_BACKEND_UNMAPPED', 'info', 'Handover is enabled but a volume type has no RHOSO backend mapping.', 'Add the volume type to the handover backend map.', ['storage_handover']);
  // like the API (SDD §9.3, ADV-18): a database VM verified only by its boot
  if (isDatabase(vm) && probesFor(vm.source_id, plan).probes.length === 0) {
    const suggested = suggestProbes(vm);
    add(
      'APP_PROBE_MISSING',
      'warning',
      `${vm.name} is a database VM without an application probe: verification would check only that it booted.${suggested.length ? ` Suggested: ${describeProbes(suggested)}.` : ''}`,
      "Add an application probe for its service to the plan, for every VM or for this one (probe_overrides), or use the suggested probe on the plan page.",
    );
  }
  return findings;
}

// -------------------------------------------------------------------------------------------
// Plans and migrations
// -------------------------------------------------------------------------------------------

export function defaultPlanFields(now: number) {
  return {
    description: null,
    mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
    default_strategy: 'auto' as const,
    strategy_overrides: {},
    selection_policy: 'min_downtime' as const,
    downtime_slo_s: 600,
    require_approval: true,
    require_distinct_approver: true,
    auto_cutover: false,
    cutover_window: null,
    keep_warm_interval_s: 900,
    convergence_threshold_bytes: 1073741824,
    max_sync_passes: 5,
    estimator_overrides: {},
    link_bps: 131072000,
    handover: { enabled: false, backend_map: {} },
    verification: {
      tcp_ports: [22],
      windows_tcp_ports: [3389],
      probe_address: 'fixed' as const,
      console_success_patterns: ['login:', 'Cloud-init v\\. .* finished', 'Reached target .*Multi-User'],
      timeout_s: 600,
      auto_rollback: true,
      use_advisor: true,
      app_probes: [],
    },
    probe_overrides: {},
    finalize_warning_h: 72,
    prestage_resources: ['networks', 'subnets', 'routers', 'router_interfaces', 'security_groups', 'security_group_rules'],
    port_creation_mode: 'nova' as const,
    nic_overrides: {} as Plan['nic_overrides'],
    waves: [],
    // what the API derives from the migrations (SDD §8.1); the mock sets it on every response
    derived_status: 'draft' as const,
    created_at: iso(now),
    updated_at: iso(now),
  };
}

interface MigrationSpec {
  id: string;
  vm: string;
  strategy: Strategy;
  phase: Phase;
  wave?: string | null;
  /** Seconds ago the current phase was entered. */
  enteredAgo: number;
  progress?: number;
  passes?: Array<{ kind: SyncPass['kind']; changedGiB: number; durationS: number; open?: boolean }>;
  downtimeStartedAgo?: number | null;
  downtimeS?: number | null;
  approvals?: Array<{ actor: string; ago: number; comment?: string }>;
  cutoverRequested?: boolean;
  error?: string | null;
  reviewReason?: string | null;
  notes?: Array<Omit<AdvisorNote, 'created_at'> & { ago: number }>;
  /** The strategy recommendation recorded when the migration was validated (SDD §14.4), and what a person decided later (§14.6). */
  rec?: {
    option: Strategy | null;
    confidence: number | null;
    status: StrategyDecision;
    deterministic: Strategy;
    fallback?: string;
    policyId?: string;
    then?: { kind: 'approve'; user: string; after: number } | { kind: 'override'; user: string; option: Strategy; reason: string; after: number };
  };
  attempts?: number;
  destination?: string | null;
  checkpoint?: string | null;
}

interface PlanSpec {
  id: string;
  name: string;
  description: string;
  source: string;
  destination: string;
  status: Plan['status'];
  createdAgo: number;
  overrides?: Partial<Plan>;
  waves: Array<{ id: string; name: string; vms: string[]; depends_on?: string[]; max_parallel?: number }>;
  migrations: MigrationSpec[];
}

const H = 3600;
const D = 86_400;

const PLAN_SPECS: PlanSpec[] = [
  {
    id: 'plan-4f2a9c1e',
    name: 'DC1 → RHOSO production rollout',
    description: 'RHOSP 17.1 DC1 tenants to RHOSO 18.0; warm pre-copy for databases, pilot first.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'running',
    createdAgo: 3 * D,
    overrides: {
      mappings: { networks: { 'tenant-infra': 'tenant-infra' }, flavors: { 'g1.a10': 'm1.large' }, volume_types: { 'tripleo-ceph': 'ceph-hdd', 'tripleo-ceph-ssd': 'ceph-ssd' }, projects: {} },
      // PRD G1: warm cutovers within 15 minutes.
      downtime_slo_s: 900,
    },
    waves: [
      { id: 'wave-1', name: 'Pilot — stateless web', vms: ['os-0a11', 'os-0a12', 'os-0a13'], max_parallel: 3 },
      { id: 'wave-2', name: 'Databases — warm pre-copy', vms: ['os-0d51', 'os-0d52', 'os-0e64', 'os-0c41'], max_parallel: 4 },
      { id: 'wave-3', name: 'Middleware', vms: ['os-0b22', 'os-0b32', 'os-0e62'], depends_on: ['wave-1', 'wave-2'], max_parallel: 5 },
    ],
    migrations: [
      {
        rec: { option: 'warm', confidence: 0.74, status: 'auto_applied', deterministic: 'cold' },
        id: 'mig-3c1a0f9e21', vm: 'os-0a11', strategy: 'warm', phase: 'completed', wave: 'wave-1', enteredAgo: 2 * H, progress: 100,
        passes: [{ kind: 'full', changedGiB: 12, durationS: 128 }, { kind: 'delta', changedGiB: 0.42, durationS: 84 }, { kind: 'final', changedGiB: 0.05, durationS: 81 }],
        downtimeStartedAgo: 2 * H + 212, downtimeS: 212, approvals: [{ actor: 'sari', ago: 2 * H + 300, comment: 'Pilot approved in CAB-2291' }], cutoverRequested: true,
        destination: 'b7e2c0f4-1d2a-4c55-9a1e-6f4c2d8e9a01', checkpoint: 'verified',
        notes: [{ kind: 'classification', source: 'rules', summary: 'Classified as stateless_web: name matches ^web- and tag tier=web.', confidence: null, data: { tier: 'stateless_web' }, ago: 3 * D }],
      },
      {
        rec: { option: 'warm', confidence: 0.81, status: 'auto_applied', deterministic: 'cold' },
        id: 'mig-3c1a0f9e22', vm: 'os-0a12', strategy: 'warm', phase: 'verifying', wave: 'wave-1', enteredAgo: 70, progress: 100,
        passes: [{ kind: 'full', changedGiB: 14, durationS: 131 }, { kind: 'delta', changedGiB: 0.51, durationS: 86 }, { kind: 'final', changedGiB: 0.06, durationS: 83 }],
        downtimeStartedAgo: 250, approvals: [{ actor: 'sari', ago: 400, comment: 'Checked the pre-flight findings with the web team' }], cutoverRequested: true, destination: '2f9d6b1e-77c4-4f0e-8d3a-0c5b9e7a6d12', checkpoint: 'cutover',
        notes: [{ kind: 'verification', source: 'jev', summary: 'Both claims verified: the guest finished booting and no filesystem errors were reported.', confidence: 0.88, data: { results: [{ claim: 'The guest operating system finished booting', verdict: 'verified' }] }, ago: 30 }],
      },
      {
        rec: { option: 'warm', confidence: 0.69, status: 'auto_applied', deterministic: 'cold' },
        id: 'mig-3c1a0f9e23', vm: 'os-0a13', strategy: 'warm', phase: 'cutover', wave: 'wave-1', enteredAgo: 95, progress: 62,
        passes: [{ kind: 'full', changedGiB: 13, durationS: 129 }, { kind: 'delta', changedGiB: 0.47, durationS: 85 }, { kind: 'final', changedGiB: 0.05, durationS: 0, open: true }],
        downtimeStartedAgo: 95, approvals: [{ actor: 'sari', ago: 180, comment: 'Inside the change window agreed with the owner' }], cutoverRequested: true, checkpoint: 'precopy',
      },
      {
        id: 'mig-5d7e2b4a10', vm: 'os-0d51', strategy: 'warm', phase: 'precopy', wave: 'wave-2', enteredAgo: 31 * 60, progress: 37,
        passes: [{ kind: 'full', changedGiB: 0, durationS: 0, open: true }], checkpoint: 'prestage',
        rec: { option: 'warm', confidence: 0.67, status: 'auto_applied', deterministic: 'warm' },
      },
      {
        id: 'mig-5d7e2b4a11', vm: 'os-0d52', strategy: 'warm', phase: 'syncing', wave: 'wave-2', enteredAgo: 14 * 60, progress: 48,
        rec: { option: null, confidence: null, status: 'fallback_rules', deterministic: 'warm', fallback: 'advisor unavailable: jev_decide timed out after 20 s' },
        passes: [{ kind: 'full', changedGiB: 316, durationS: 2590 }, { kind: 'delta', changedGiB: 12.6, durationS: 1132 }, { kind: 'delta', changedGiB: 0, durationS: 0, open: true }],
        checkpoint: 'precopy',
      },
      {
        id: 'mig-5d7e2b4a12', vm: 'os-0e64', strategy: 'warm', phase: 'awaiting_cutover', wave: 'wave-2', enteredAgo: 22 * 60, progress: 100,
        passes: [{ kind: 'full', changedGiB: 110, durationS: 905 }, { kind: 'delta', changedGiB: 2.7, durationS: 412 }, { kind: 'delta', changedGiB: 0.71, durationS: 418 }],
        checkpoint: 'sync',
        notes: [{ kind: 'classification', source: 'jev', summary: 'Classified as stateful_database (margin 0.41): billing ledger writes continuously.', confidence: 0.79, data: { tier: 'stateful_database', decision: 'auto' }, ago: 3 * D }],
      },
      {
        id: 'mig-5d7e2b4a13', vm: 'os-0c41', strategy: 'cold', phase: 'ready', wave: 'wave-2', enteredAgo: 2 * H,
        checkpoint: null,
        // applied automatically, then overridden by a person with a reason (ADV-04)
        rec: {
          option: 'warm', confidence: 0.83, status: 'auto_applied', deterministic: 'cold',
          then: { kind: 'override', user: 'bayu', option: 'cold', reason: 'Domain controllers move cold: the AD replication window was rehearsed that way', after: 40 * 60 },
        },
        notes: [{ kind: 'classification', source: 'rules', summary: 'Classified as infrastructure_service: os_type windows2019 and tag app=active-directory.', confidence: null, data: { tier: 'infrastructure_service' }, ago: 3 * D }],
      },
      { id: 'mig-7a9b1c3d40', vm: 'os-0b22', strategy: 'cold', phase: 'validating', wave: 'wave-3', enteredAgo: 6 },
      { id: 'mig-7a9b1c3d41', vm: 'os-0b32', strategy: 'warm', phase: 'pending', wave: 'wave-3', enteredAgo: 40 * 60 },
      { id: 'mig-7a9b1c3d42', vm: 'os-0e62', strategy: 'cold', phase: 'blocked', wave: 'wave-3', enteredAgo: 2 * H },
    ],
  },
  {
    id: 'plan-7b3e0d52',
    name: 'VMware exit — ERP',
    description: 'vCenter HQ ERP cluster; CBT-enabled VMs run warm through vmware-migration-kit.',
    source: 'vcenter-hq',
    destination: 'rhoso-prod',
    status: 'paused',
    createdAgo: 6 * D,
    overrides: { mappings: { networks: { 'VM Network ERP': 'erp-net' }, flavors: {}, volume_types: {}, projects: { erp: 'erp' } }, downtime_slo_s: 600 },
    waves: [
      { id: 'wave-1', name: 'ERP application tier', vms: ['vm-5001', 'vm-5005'], max_parallel: 2 },
      { id: 'wave-2', name: 'ERP data and files', vms: ['vm-5002', 'vm-5003', 'vm-5004'], depends_on: ['wave-1'], max_parallel: 2 },
    ],
    migrations: [
      {
        id: 'mig-9e4f6a8b50', vm: 'vm-5001', strategy: 'vmware_warm', phase: 'syncing', wave: 'wave-1', enteredAgo: 9 * 60, progress: 71,
        passes: [{ kind: 'full', changedGiB: 64, durationS: 520 }, { kind: 'delta', changedGiB: 0, durationS: 0, open: true }],
        checkpoint: 'precopy',
      },
      {
        id: 'mig-9e4f6a8b51', vm: 'vm-5005', strategy: 'vmware_warm', phase: 'ready', wave: 'wave-1', enteredAgo: 5 * H,
        // below the threshold, then approved by a person: the recommended option runs
        rec: { option: 'vmware_warm', confidence: 0.55, status: 'below_threshold', deterministic: 'vmware_cold', then: { kind: 'approve', user: 'sari', after: 20 * 60 } },
      },
      {
        id: 'mig-9e4f6a8b52', vm: 'vm-5002', strategy: 'vmware_warm', phase: 'ready', wave: 'wave-2', enteredAgo: 5 * H,
        approvals: [{ actor: 'sari', ago: 4 * H, comment: 'Pre-approved for the Saturday window' }],
      },
      { id: 'mig-9e4f6a8b53', vm: 'vm-5003', strategy: 'vmware_cold', phase: 'ready', wave: 'wave-2', enteredAgo: 5 * H },
      { id: 'mig-9e4f6a8b54', vm: 'vm-5004', strategy: 'vmware_cold', phase: 'pending', wave: 'wave-2', enteredAgo: 5 * H },
    ],
  },
  {
    id: 'plan-c81d44a0',
    name: 'Shared Ceph handover — analytics',
    description: 'RHOSO uses the existing external Ceph cluster; Spark nodes are handed over by Cinder unmanage/manage.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'validated',
    createdAgo: 1 * D,
    overrides: {
      handover: { enabled: true, backend_map: { 'ceph-ssd': 'hostgroup@ceph-ssd#volumes-ssd' } },
      cutover_window: null,
      downtime_slo_s: 600,
    },
    waves: [{ id: 'wave-1', name: 'Spark cluster', vms: ['os-0f71', 'os-0f72', 'os-0f73', 'os-0f74'], max_parallel: 4 }],
    migrations: [
      {
        id: 'mig-b2c4d6e870', vm: 'os-0f71', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H,
        notes: [{ kind: 'strategy', source: 'jev', summary: 'Storage handover recommended: metadata-only cutover (≈ 4 min) regardless of the 540 GiB of disks.', confidence: 0.71, data: { selected: 'storage_handover', applied: true, deterministic: 'storage_handover', candidates: ['storage_handover', 'warm'], probabilities: { storage_handover: 0.71, warm: 0.29 } }, ago: 3 * H }],
      },
      {
        id: 'mig-b2c4d6e871', vm: 'os-0f72', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H,
        // the Finance policy asks a person first: the migration waits (ADV-04)
        rec: { option: 'storage_handover', confidence: 0.86, status: 'pending_approval', deterministic: 'storage_handover', policyId: FINANCE_POLICY_ID },
      },
      { id: 'mig-b2c4d6e872', vm: 'os-0f73', strategy: 'storage_handover', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H },
      { id: 'mig-b2c4d6e873', vm: 'os-0f74', strategy: 'warm', phase: 'ready', wave: 'wave-1', enteredAgo: 3 * H },
    ],
  },
  {
    id: 'plan-0e9f6a17',
    name: 'Community lab cleanup',
    description: 'Retire the 2023.1 lab cloud. Draft: the source provider is currently failing its health check.',
    source: 'community-lab',
    destination: 'rhoso-prod',
    status: 'draft',
    createdAgo: 2 * H,
    overrides: { require_approval: false },
    waves: [],
    migrations: [
      { id: 'mig-c3d5e7f980', vm: 'lab-2b01', strategy: 'cold', phase: 'pending', enteredAgo: 2 * H },
      { id: 'mig-c3d5e7f981', vm: 'lab-2b02', strategy: 'cold', phase: 'pending', enteredAgo: 2 * H },
    ],
  },
  {
    id: 'plan-2d5c8b93',
    name: 'Pilot — static content',
    description: 'First production pilot: static web content and cache.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'completed',
    createdAgo: 9 * D,
    overrides: { downtime_slo_s: 600 },
    waves: [{ id: 'wave-1', name: 'Pilot', vms: ['os-0e65', 'os-0a14'], max_parallel: 2 }],
    migrations: [
      {
        id: 'mig-d4e6f8a090', vm: 'os-0e65', strategy: 'cold', phase: 'finalized', wave: 'wave-1', enteredAgo: 7 * D, progress: 100,
        // the Advisor spec's example (ADV-01): Jev's warm at 57% is under the 60% threshold, so cold is kept
        rec: { option: 'warm', confidence: 0.57, status: 'below_threshold', deterministic: 'cold' },
        // a cold copy is recorded as a full pass while the source is stopped, as the API's executors record it
        passes: [{ kind: 'full', changedGiB: 6, durationS: 49 }],
        downtimeStartedAgo: 8 * D + 900, downtimeS: 418, approvals: [{ actor: 'sari', ago: 8 * D + 1200, comment: 'Approved in CAB-2217; the owner signed off the downtime' }], cutoverRequested: true, destination: '0c4e8a2b-5f1d-4e93-b7a6-3d2f1e0c9b88', checkpoint: 'finalize',
      },
      {
        id: 'mig-d4e6f8a091', vm: 'os-0a14', strategy: 'warm', phase: 'completed', wave: 'wave-1', enteredAgo: 8 * D, progress: 100,
        rec: { option: 'warm', confidence: 0.88, status: 'auto_applied', deterministic: 'cold' },
        passes: [{ kind: 'full', changedGiB: 9, durationS: 96 }, { kind: 'delta', changedGiB: 0.31, durationS: 63 }, { kind: 'final', changedGiB: 0.04, durationS: 61 }],
        downtimeStartedAgo: 8 * D + 188, downtimeS: 188, approvals: [{ actor: 'sari', ago: 8 * D + 600, comment: 'Backups verified this morning; cut over in the window' }], cutoverRequested: true, destination: '9a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d', checkpoint: 'verify',
      },
    ],
  },
  {
    id: 'plan-95a7e3f1',
    name: 'Legacy apps',
    description: 'End-of-life workloads; cold migration with manual verification.',
    source: 'rhosp17-dc1',
    destination: 'rhoso-prod',
    status: 'failed',
    createdAgo: 2 * D,
    overrides: { verification: { tcp_ports: [22, 8080], windows_tcp_ports: [3389], probe_address: 'fixed', console_success_patterns: ['login:'], timeout_s: 600, auto_rollback: false, use_advisor: true, app_probes: [] }, downtime_slo_s: 900 },
    waves: [{ id: 'wave-1', name: 'Legacy', vms: ['os-0e61', 'os-0e63', 'os-0e66', 'os-0e67'], max_parallel: 2 }],
    migrations: [
      {
        id: 'mig-e5f7a9b1a0', vm: 'os-0e61', strategy: 'cold', phase: 'failed', wave: 'wave-1', enteredAgo: 33 * 60, progress: 100,
        downtimeStartedAgo: 41 * 60, approvals: [{ actor: 'sari', ago: 45 * 60, comment: 'Agreed with the application team for this evening' }], cutoverRequested: true, destination: '5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9', checkpoint: 'cutover', attempts: 1,
        error: 'Verification failed: console pattern "login:" not matched within 600 s; tcp:22 connection refused.',
        reviewReason: 'Jev contradicted "The guest operating system finished booting" (console shows a kernel panic).',
        notes: [
          { kind: 'screen', source: 'jev', summary: 'Console excerpt blocked: it contained instructions aimed at an AI agent (injection probability 0.97); dropped from evidence.', confidence: 0.97, data: { action: 'block', injection: 0.97 }, ago: 34 * 60 },
          { kind: 'verification', source: 'jev', summary: 'Contradicted: "The guest operating system finished booting" — console shows "Kernel panic - not syncing: VFS: Unable to mount root fs".', confidence: 0.91, data: { verdict: 'contradicted', action: 'auto' }, ago: 33 * 60 },
          {
            kind: 'similar_incidents', source: 'memory', summary: '2 similar incidents: RHEL 6 guests lacked virtio drivers in the initramfs after cold import.', confidence: null,
            data: { hits: [
              { title: 'RHEL 6 kernel panic after os-migrate import', content: 'Rebuild the initramfs with virtio_blk and virtio_pci (dracut --add-drivers) before cutover; retry succeeded.', score: 0.83 },
              { title: 'Payroll VM unbootable on RHOSO (VFS mount)', content: 'Root device changed from /dev/vda to /dev/sda in fstab; switched to UUID mounts.', score: 0.71 },
            ] },
            ago: 32 * 60,
          },
        ],
      },
      {
        id: 'mig-e5f7a9b1a1', vm: 'os-0e63', strategy: 'cold', phase: 'rolling_back', wave: 'wave-1', enteredAgo: 4 * 60, progress: 40,
        downtimeStartedAgo: 19 * 60, approvals: [{ actor: 'sari', ago: 25 * 60, comment: 'Change CHG-2088 approved; DBA on call for the cutover' }], cutoverRequested: true, checkpoint: 'cutover',
        error: 'Import failed: volume attach timed out on the destination conversion host.',
      },
      {
        id: 'mig-e5f7a9b1a2', vm: 'os-0e66', strategy: 'cold', phase: 'rolled_back', wave: 'wave-1', enteredAgo: 3 * H, progress: 0,
        downtimeStartedAgo: 3 * H + 1260, downtimeS: 1260, approvals: [{ actor: 'sari', ago: 4 * H, comment: 'Approved for the maintenance window; rollback plan reviewed' }], cutoverRequested: true, checkpoint: 'rollback', attempts: 1,
        error: 'Rolled back by bayu: tcp:8080 closed after boot; application config references the old database host.',
      },
      { id: 'mig-e5f7a9b1a3', vm: 'os-0e67', strategy: 'cold', phase: 'cancelled', wave: 'wave-1', enteredAgo: 1 * D },
    ],
  },
];

/** Phase walk used to synthesize history for a migration that is currently in `phase`. */
function walkTo(strategy: Strategy, phase: Phase): Phase[] {
  const warm = strategy === 'warm' || strategy === 'vmware_warm';
  const success: Phase[] = warm
    ? ['pending', 'validating', 'ready', 'precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'completed', 'finalized']
    : ['pending', 'validating', 'ready', 'cutover', 'verifying', 'completed', 'finalized'];
  switch (phase) {
    case 'blocked':
      return ['pending', 'validating', 'blocked'];
    case 'failed':
      return [...success.slice(0, success.indexOf('verifying') + 1), 'failed'];
    case 'rolling_back':
      return [...success.slice(0, success.indexOf('cutover') + 1), 'rolling_back'];
    case 'rolled_back':
      return [...success.slice(0, success.indexOf('verifying') + 1), 'rolling_back', 'rolled_back'];
    case 'cancelled':
      return ['pending', 'validating', 'ready', 'cancelled'];
    default: {
      const index = success.indexOf(phase);
      return index === -1 ? [phase] : success.slice(0, index + 1);
    }
  }
}

const REASONS: Partial<Record<Phase, string>> = {
  validating: 'plan validation started',
  ready: 'pre-flight passed',
  blocked: 'blocker findings present',
  precopy: 'wave started',
  syncing: 'pass 1 complete; delta passes until convergence',
  awaiting_cutover: 'converged: last delta below the threshold',
  cutover: 'cutover gate satisfied (approved, window open)',
  verifying: 'destination server created',
  completed: 'verification passed',
  finalized: 'finalized by approver',
  failed: 'step failed',
  rolling_back: 'rollback requested',
  rolled_back: 'source VM running again',
  cancelled: 'cancelled by operator',
};

/**
 * Phase entries along `path`: known milestones (`anchors`: pass boundaries, downtime start/end, the
 * current phase) are used as-is; the others are interpolated between their neighbours.
 */
function synthesizeHistory(path: Phase[], anchors: Map<Phase, number>, createdAt: number, actorFor: (p: Phase) => string): PhaseChange[] {
  const times: Array<number | null> = path.map((phase, i) => (i === 0 ? createdAt : (anchors.get(phase) ?? null)));
  for (let i = 1; i < times.length; i += 1) {
    if (times[i] !== null) continue;
    let j = i + 1;
    while (j < times.length && times[j] === null) j += 1;
    const prev = times[i - 1] as number;
    const next = j < times.length ? (times[j] as number) : prev + 60_000 * (j - i + 1);
    const steps = j - i + 1;
    for (let k = i; k < j; k += 1) times[k] = prev + ((next - prev) * (k - i + 1)) / steps;
    i = j - 1;
  }
  for (let i = 1; i < times.length; i += 1) {
    if ((times[i] as number) <= (times[i - 1] as number)) times[i] = (times[i - 1] as number) + 1000;
  }
  return path.map((to, i) => ({
    from_phase: i === 0 ? null : (path[i - 1] ?? null),
    to_phase: to,
    at: iso(times[i] as number),
    reason: i === 0 ? 'migration created' : (REASONS[to] ?? 'transition'),
    actor: actorFor(to),
  }));
}

interface ScheduledPass {
  spec: NonNullable<MigrationSpec['passes']>[number];
  start: number;
  end: number | null;
}

/**
 * Lays warm passes out backwards from the point where the migration converged (or the open pass
 * started), and the final pass right after the source VM stopped.
 */
function schedulePasses(m: MigrationSpec, enteredAt: number, downtimeStartedAt: number | null): ScheduledPass[] {
  const specs = m.passes ?? [];
  // the pass a cutover runs with the source stopped: a warm migration's final sync, a cold one's copy (SDD §4.2)
  const singleShot = m.strategy === 'cold' || m.strategy === 'vmware_cold';
  const atCutover = (p: (typeof specs)[number]) => p.kind === 'final' || (singleShot && p.kind === 'full');
  const open = specs.find((p) => p.open && !atCutover(p));
  const final = specs.find(atCutover);
  const done = specs.filter((p) => !p.open && !atCutover(p));
  let cursor: number;
  if (open) cursor = enteredAt - 60_000;
  else if (m.phase === 'awaiting_cutover') cursor = enteredAt - 30_000;
  else if (downtimeStartedAt !== null) cursor = downtimeStartedAt - 20 * 60_000;
  else cursor = enteredAt;
  const scheduled: ScheduledPass[] = [];
  for (const spec of [...done].reverse()) {
    const end = cursor;
    const start = end - spec.durationS * 1000;
    scheduled.unshift({ spec, start, end });
    cursor = start - 60_000;
  }
  if (open) scheduled.push({ spec: open, start: enteredAt, end: null });
  if (final && downtimeStartedAt !== null) {
    const start = downtimeStartedAt + 20_000;
    scheduled.push({ spec: final, start, end: final.open ? null : start + final.durationS * 1000 });
  }
  return scheduled;
}

/** Phases a warm migration reaches only once its passes converged (SDD §5.3). */
const PAST_CONVERGENCE: ReadonlySet<Phase> = new Set(['awaiting_cutover', 'cutover', 'verifying', 'completed', 'finalized']);

/**
 * The rule that ended a warm fixture's passes, in the API's order (SDD §5.3): the threshold (not for
 * CBT passes), an estimate within the SLO, the pass limit — so the record and its numbers agree (§5.5).
 */
function fixtureConvergedBy(strategy: Strategy, phase: Phase, passes: SyncPass[], estimate: Estimate | null, plan: Plan): ConvergedBy | null {
  if ((strategy !== 'warm' && strategy !== 'vmware_warm') || !PAST_CONVERGENCE.has(phase)) return null;
  const last = passes.filter((p) => p.kind !== 'final' && p.ended_at !== null).at(-1);
  if (!last) return null;
  if (strategy !== 'vmware_warm' && last.bytes_changed <= plan.convergence_threshold_bytes) return 'threshold';
  if (estimate && estimate.downtime_s <= plan.downtime_slo_s) return 'slo';
  return last.number >= plan.max_sync_passes ? 'max_passes' : null;
}

/**
 * The last verification of a fixture (SDD §7.5): passed for one that completed, failed for one whose
 * error says verification failed; no application probe, as the fixture plans configure none.
 */
function fixtureVerification(phase: Phase, endedAt: number | null, error: string | null): VerificationRecord | null {
  const passed = phase === 'completed' || phase === 'finalized';
  if (!passed && !error?.startsWith('Verification failed')) return null;
  const at = iso(endedAt ?? Date.parse('2026-10-08T00:00:00Z'));
  const checks = passed
    ? [
        { name: 'server_active', ok: true, detail: 'status ACTIVE' },
        { name: 'ports_up', ok: true, detail: '1/1 ports ACTIVE' },
        { name: 'tcp:22', ok: true, detail: 'connected in 4 ms' },
        { name: 'console', ok: true, detail: "matched 'login:'" },
      ]
    : [
        { name: 'server_active', ok: true, detail: 'status ACTIVE' },
        { name: 'ports_up', ok: true, detail: '1/1 ports ACTIVE' },
        { name: 'tcp:22', ok: false, detail: 'connection refused' },
        { name: 'console', ok: false, detail: 'no success pattern in the last 200 console lines' },
      ];
  return { at, passed, checks, probes: [], warnings: [] };
}

export interface Fixtures {
  providers: Provider[];
  inventories: Record<string, VMRef[] | DestinationInventory>;
  plans: Plan[];
  migrations: Migration[];
  /** Stored recommendations (document kind `recommendation`, SDD §14.4). */
  recommendations: AdvisorRecommendation[];
  /** Redacted advisor inputs (document kind `advisor_snapshot`, SDD §14.5). */
  snapshots: AdvisorSnapshot[];
  /** Stored advisor policies (document kind `advisor_policy`, SDD §14.6). */
  policies: AdvisorPolicy[];
  events: Event[];
}

export function buildFixtures(now: number): Fixtures {
  const providers = buildProviders(now);
  const inventories = buildInventories();
  const plans: Plan[] = [];
  const migrations: Migration[] = [];
  const recommendations: AdvisorRecommendation[] = [];
  const snapshots: AdvisorSnapshot[] = [];
  const pending: Array<Omit<Event, 'seq'>> = [];
  const policyAt = iso(now - 4 * D * 1000);
  const policies: AdvisorPolicy[] = [
    {
      id: FINANCE_POLICY_ID,
      name: 'Finance — approval required',
      scope: { provider_ids: ['rhosp17-dc1'], plan_ids: [] },
      decision_types: ['migration_strategy'],
      mode: 'always_require_approval',
      threshold: JEV_MIN_CONFIDENCE,
      min_evidence_for_auto_apply: 0,
      require_override_reason: true,
      require_distinct_approver: true,
      created_at: policyAt,
      updated_at: policyAt,
      updated_by: 'rina',
    },
  ];
  pending.push({ ts: policyAt, kind: 'policy.created', plan_id: null, migration_id: null, actor: 'user:rina', message: 'advisor policy pol-finance created', data: { target_id: FINANCE_POLICY_ID, before: null, after: policies[0] } });
  const emit = (e: Omit<Event, 'seq'>) => pending.push(e);

  for (const spec of PLAN_SPECS) {
    const createdAt = now - spec.createdAgo * 1000;
    const source = providers.find((p) => p.id === spec.source);
    const destination = providers.find((p) => p.id === spec.destination);
    const vms = (inventories[spec.source] ?? []) as VMRef[];
    const dstInventory = inventories[spec.destination] as DestinationInventory;
    if (!source || !destination) continue;

    const plan: Plan = {
      id: spec.id,
      name: spec.name,
      source_provider_id: spec.source,
      destination_provider_id: spec.destination,
      vm_ids: spec.migrations.map((m) => m.vm),
      status: spec.status,
      ...defaultPlanFields(createdAt),
      ...spec.overrides,
      description: spec.description,
      waves: spec.waves.map((w, i) => ({ id: w.id, name: w.name, order: i + 1, vm_ids: w.vms, depends_on: w.depends_on ?? [], max_parallel: w.max_parallel ?? 5 })),
      updated_at: iso(now - 5 * 60_000),
    };
    if (spec.id === 'plan-4f2a9c1e') plan.cutover_window = { start: iso(now - 1 * H * 1000), end: iso(now + 5 * H * 1000) };
    if (spec.id === 'plan-7b3e0d52') plan.cutover_window = { start: iso(now + 2 * D * 1000), end: iso(now + 2 * D * 1000 + 6 * H * 1000) };
    plans.push(plan);
    emit({ ts: iso(createdAt), kind: 'plan.created', plan_id: plan.id, migration_id: null, actor: 'rina', message: `Plan "${plan.name}" created with ${plan.vm_ids.length} VMs`, data: { status: 'draft' } });

    const selectedNames = plan.vm_ids.map((id) => vms.find((v) => v.source_id === id)?.name ?? id);
    for (const m of spec.migrations) {
      const vm = vms.find((v) => v.source_id === m.vm);
      if (!vm) continue;
      const findings = preflight(vm, source.kind, plan, dstInventory.networks, selectedNames);
      const elig = eligibility(vm, source.kind, plan, source, destination, findings);
      const estimates = (Object.keys(elig) as Strategy[]).map((s) => estimate(vm, s, plan, elig[s] ?? []));
      const chosen = estimates.find((e) => e.strategy === m.strategy) ?? null;
      const enteredAt = now - m.enteredAgo * 1000;
      const migCreatedAt = Math.min(createdAt + 60_000, enteredAt - 60_000);
      const path = walkTo(m.strategy, m.phase);
      const downtimeStartedAt = m.downtimeStartedAgo != null ? now - m.downtimeStartedAgo * 1000 : null;
      const downtimeEndedAt = downtimeStartedAt !== null && m.downtimeS != null ? downtimeStartedAt + m.downtimeS * 1000 : null;
      const scheduled = schedulePasses(m, enteredAt, downtimeStartedAt);

      // Milestones that phase entries must line up with.
      const anchors = new Map<Phase, number>();
      const firstPass = scheduled[0];
      if (firstPass && path.includes('precopy')) anchors.set('precopy', firstPass.start);
      const fullPass = scheduled.find((x) => x.spec.kind === 'full');
      if (fullPass?.end != null && path.includes('syncing')) anchors.set('syncing', fullPass.end + 5_000);
      const lastDelta = [...scheduled].reverse().find((x) => x.spec.kind !== 'final' && x.end !== null);
      if (lastDelta?.end != null && path.includes('awaiting_cutover') && m.phase !== 'precopy' && m.phase !== 'syncing') {
        anchors.set('awaiting_cutover', lastDelta.end + 5_000);
      }
      if (downtimeStartedAt !== null && path.includes('cutover')) anchors.set('cutover', downtimeStartedAt);
      if (downtimeEndedAt !== null && path.includes('completed')) anchors.set('completed', downtimeEndedAt);
      if (downtimeEndedAt !== null && path.includes('rolled_back')) anchors.set('rolled_back', downtimeEndedAt);
      if (!anchors.has(m.phase)) anchors.set(m.phase, enteredAt);
      const history = synthesizeHistory(path, anchors, migCreatedAt, (p) =>
        p === 'finalized' ? 'sari' : p === 'cancelled' || p === 'rolling_back' ? 'bayu' : 'orchestrator',
      );
      const bytesTotal = m.strategy === 'storage_handover' ? 0 : vm.used_bytes;
      const progress = m.progress ?? 0;
      const syncPasses: SyncPass[] = scheduled.map(({ spec: p, start, end }, i) => {
        const changed = Math.round(p.changedGiB * GiB);
        return {
          number: i + 1,
          kind: p.kind,
          started_at: iso(start),
          ended_at: end === null ? null : iso(end),
          bytes_scanned: end === null ? Math.round((vm.disk_bytes * progress) / 100) : vm.disk_bytes,
          bytes_changed: end === null ? 0 : changed,
          bytes_transferred: end === null ? Math.round(((bytesTotal * progress) / 100) * (p.kind === 'full' ? 1 : 0.05)) : Math.round(changed * 0.98),
          duration_s: end === null ? null : p.durationS,
        };
      });
      const migration: Migration = {
        id: m.id,
        plan_id: plan.id,
        wave_id: m.wave ?? null,
        vm,
        strategy: m.strategy,
        phase: m.phase,
        phase_history: history,
        progress_pct: progress,
        // like the API (SDD §4.2): the disk's used bytes, and every pass that ended plus the running one
        // (a migration without passes counts its copy's progress; a storage handover copies nothing)
        bytes_total: vm.used_bytes,
        bytes_transferred: syncPasses.length ? syncPasses.reduce((sum, p) => sum + p.bytes_transferred, 0) : Math.round((bytesTotal * progress) / 100),
        // like the API, only passes that ended are listed (SDD §4.2); a running one counts in bytes_transferred
        sync_passes: syncPasses.filter((p) => p.ended_at !== null),
        sync_bytes_dropped: 0,
        estimate: chosen,
        estimates,
        // a delta pass calibrates the per-stream scan rate (SDD §9.1); pass 1 alone does not
        observed_scan_bps: syncPasses.filter((p) => p.kind !== 'full' && p.ended_at !== null).length > 0 ? 545 * MiB : null,
        resolved_mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
        findings,
        checkpoint: m.checkpoint ?? null,
        downtime_started_at: downtimeStartedAt === null ? null : iso(downtimeStartedAt),
        downtime_ended_at: downtimeEndedAt === null ? null : iso(downtimeEndedAt),
        actual_downtime_s: m.downtimeS ?? null,
        approvals: (m.approvals ?? []).map((a) => ({ actor: a.actor, at: iso(now - a.ago * 1000), comment: a.comment ?? null })),
        cutover_requested: m.cutoverRequested ?? false,
        // maker-checker (SDD §5.4, ADV-17): requested by one person, approved by another, before the approval
        cutover_requested_by: m.cutoverRequested ? REQUESTED_BY : null,
        cutover_requested_at: m.cutoverRequested ? iso(now - (Math.max(...(m.approvals ?? []).map((a) => a.ago), m.enteredAgo) + 120) * 1000) : null,
        force_window: false,
        advisor_notes: (m.notes ?? []).map(({ ago, ...note }) => ({ ...note, created_at: iso(now - ago * 1000) })),
        advisor_hold: null,
        converged_by: fixtureConvergedBy(m.strategy, m.phase, syncPasses, chosen, plan),
        force_cutover: null,
        data_issues: [],
        downtime_model_s: null,
        verification: fixtureVerification(m.phase, downtimeEndedAt, m.error ?? null),
        suggested_probes: m.phase === 'pending' ? [] : suggestProbes(vm),
        source_running_at: null,
        review_required: Boolean(m.reviewReason),
        review_reason: m.reviewReason ?? null,
        destination_server_id: m.destination ?? null,
        error: m.error ?? null,
        attempts: m.attempts ?? 0,
        // a fixture's earlier attempts ran its strategy (SDD §14.4)
        attempt_strategies: Array<Strategy>(m.attempts ?? 0).fill(m.strategy),
        created_at: iso(migCreatedAt),
        updated_at: iso(enteredAt),
      };
      migrations.push(migration);

      emit({ ts: iso(migCreatedAt), kind: 'migration.created', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: migration created (${m.strategy})`, data: { strategy: m.strategy } });
      for (const change of history.slice(1)) {
        emit({ ts: change.at, kind: 'migration.phase', plan_id: plan.id, migration_id: m.id, actor: change.actor, message: `${vm.name}: ${change.from_phase} → ${change.to_phase}`, data: { from_phase: change.from_phase, to_phase: change.to_phase, reason: change.reason } });
      }
      for (const pass of syncPasses.filter((p) => p.ended_at)) {
        emit({ ts: pass.ended_at ?? pass.started_at, kind: 'migration.sync_pass', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: pass ${pass.number} (${pass.kind}) changed ${(pass.bytes_changed / GiB).toFixed(2)} GiB`, data: { pass } });
      }
      for (const approval of migration.approvals) {
        emit({ ts: approval.at, kind: 'migration.approved', plan_id: plan.id, migration_id: m.id, actor: approval.actor, message: `${vm.name}: approved by ${approval.actor}`, data: { comment: approval.comment } });
      }
      if (migration.downtime_started_at) {
        emit({ ts: migration.downtime_started_at, kind: 'migration.downtime_started', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: source VM stopped — downtime clock started`, data: {} });
      }
      if (migration.downtime_ended_at) {
        emit({ ts: migration.downtime_ended_at, kind: 'migration.downtime_ended', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: downtime ended after ${m.downtimeS} s`, data: { actual_downtime_s: m.downtimeS } });
      }
      if (migration.error) {
        emit({ ts: iso(enteredAt - 1000), kind: 'migration.error', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${vm.name}: ${migration.error}`, data: {} });
      }
      if (m.rec) {
        // recorded when the validation ended (SDD §14.4)
        const validated = history.find((c) => c.from_phase === 'validating')?.at ?? iso(migCreatedAt);
        const snapshot = strategySnapshot({ vm, plan, estimates, findings, scope: estimates.filter((e) => e.eligible).map((e) => e.strategy), deterministic: m.rec.deterministic, at: validated });
        snapshots.push(snapshot);
        const eligible = estimates.filter((e) => e.eligible).map((e) => e.strategy);
        const evidenceOption = m.rec.option && eligible.includes(m.rec.option as Strategy) ? m.rec.option : m.rec.deterministic;
        const kind = providers.find((p) => p.id === plan.source_provider_id)?.kind ?? 'openstack';
        const rec = strategyRecommendation({
          id: `rec-${m.id.slice(4)}01`,
          at: validated,
          migration,
          providerId: plan.source_provider_id,
          option: m.rec.option,
          confidence: m.rec.confidence,
          evidenceOption,
          evidence: buildStrategyEvidence(vm, plan, kind, estimates, findings, evidenceOption, null, migration.id),
          similarIncidents: FIXTURE_INCIDENTS[evidenceOption] ?? [],
          options: eligible,
          status: m.rec.status,
          deterministic: m.rec.deterministic,
          fallback: m.rec.fallback,
          snapshotSha256: snapshot.sha256,
          policyId: m.rec.policyId,
          // the fixtures' validations ran as the operator (SDD §14.6): the admin may decide what waits
          requestedBy: VALIDATED_BY,
        });
        for (const event of auditEventsOf(rec)) emit({ ts: validated, plan_id: plan.id, migration_id: m.id, ...event });
        let decided = rec;
        const then = m.rec.then;
        if (then) {
          const at = iso(Date.parse(validated) + then.after * 1000);
          decided = humanDecision(rec, then.kind === 'approve' ? then : { ...then, reason: then.reason }, at);
          emit({ ts: at, plan_id: plan.id, migration_id: m.id, ...decisionEventOf(decided, rec.decision) });
          if (then.kind === 'override') plan.strategy_overrides = { ...plan.strategy_overrides, [vm.source_id]: then.option };
        }
        recommendations.push(decided);
        if (decided.decision.status === 'pending_approval') migration.advisor_hold = decided.id;
      }
      for (const note of migration.advisor_notes) {
        const kind = note.kind === 'screen' ? 'advisor.verification' : `advisor.${note.kind}`;
        emit({ ts: note.created_at, kind, plan_id: plan.id, migration_id: m.id, actor: note.source === 'memory' ? 'agentmemory' : 'advisor', message: `${vm.name}: ${note.summary}`, data: { source: note.source, confidence: note.confidence } });
      }
    }

    if (plan.status !== 'draft') emit({ ts: iso(createdAt + 30 * 60_000), kind: 'plan.validated', plan_id: plan.id, migration_id: null, actor: VALIDATED_BY, message: `Plan "${plan.name}" validated`, data: {} });
    if (['running', 'paused', 'completed', 'failed'].includes(plan.status))
      emit({ ts: iso(createdAt + 60 * 60_000), kind: 'plan.started', plan_id: plan.id, migration_id: null, actor: 'bayu', message: `Plan "${plan.name}" started`, data: {} });
    if (plan.status === 'paused') emit({ ts: iso(now - 20 * 60_000), kind: 'plan.paused', plan_id: plan.id, migration_id: null, actor: 'bayu', message: `Plan "${plan.name}" paused: vCenter degraded`, data: {} });
    if (plan.status === 'completed') emit({ ts: iso(now - 7 * D * 1000), kind: 'plan.completed', plan_id: plan.id, migration_id: null, actor: 'orchestrator', message: `Plan "${plan.name}" completed`, data: {} });
  }

  for (const provider of providers.filter((p) => p.last_checked_at)) {
    emit({ ts: provider.last_checked_at ?? iso(now), kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `${provider.name}: ${provider.status}`, data: { status: provider.status } });
  }
  // the API's audit of a refused request (SDD §13.1); 192.0.2.10 is a documentation address (RFC 5737)
  emit({
    ts: iso(now - 52 * 60_000),
    kind: 'auth.denied',
    plan_id: null,
    migration_id: null,
    actor: 'unauthenticated',
    message: 'GET /api/v1/plans: missing or invalid bearer token',
    data: { path: '/api/v1/plans', method: 'GET', reason: 'missing or invalid bearer token', required_role: 'viewer', client: '192.0.2.10' },
  });
  emit({ ts: iso(now - 7 * D * 1000 + 600_000), kind: 'memory.lesson_saved', plan_id: 'plan-2d5c8b93', migration_id: 'mig-d4e6f8a091', actor: 'agentmemory', message: 'Lesson saved: warm, <50G, 3 passes, estimate 3m 40s vs actual 3m 08s', data: { type: 'fact' } });

  for (const plan of plans) plan.derived_status = derivePlanStatus(plan, migrations);
  pending.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  const events: Event[] = pending.map((e, i) => ({ ...e, seq: i + 1 }));
  return { providers, inventories, plans, migrations, recommendations, snapshots, policies, events };
}
