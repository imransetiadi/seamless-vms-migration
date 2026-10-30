/**
 * Deterministic fixture data for the in-browser mock adapter (VITE_SEAMLESS_MOCK=1).
 * Covers every Phase, Strategy, PlanStatus, provider status, finding Severity and advisor note
 * kind/source so the whole UI can be exercised without the control plane.
 * Estimates are the API's §9.1 formula (lib/forcedEstimate), eligibility §9.2 and findings a subset of §9.3.
 */
import type {
  AdvisorNote,
  AdvisorPolicy,
  AdvisorRecommendation,
  AdvisorSnapshot,
  ConvergedBy,
  DestinationFlavor,
  DestinationInventory,
  DestinationNetwork,
  DestinationSubnet,
  Disk,
  DnsRead,
  Estimate,
  Event,
  EvidenceItem,
  ExtentUse,
  Finding,
  GpuCapacity,
  GpuDevice,
  GpuFlavor,
  GpuPool,
  Migration,
  Nic,
  NotificationChannel,
  NotificationCursor,
  NotificationSubscription,
  Phase,
  PhaseChange,
  Plan,
  ProjectCheck,
  Provider,
  ProviderKind,
  ProviderRole,
  ReasonCode,
  Severity,
  SimilarIncident,
  Strategy,
  SyncPass,
  VerificationRecord,
  VMRef,
  Wave,
} from './types';
import { DOWNTIME_PARTS } from './types';
import { canonicalJson, sha256Hex } from '../lib/canonical';
import { estimateStrategy, paramsFor } from '../lib/forcedEstimate';
import { byCodePoint, pyNames, pyRepr } from '../lib/format';
import {
  declaredProfileOf,
  gpuDevice,
  gpuFlavorOf,
  gpuKey,
  gpuPools,
  gpuRequests,
  gpuShortfalls,
  gpuUnits,
  poolName,
  requestPoolKey,
  samePool,
  vgpuProfileSize,
  vgpuRequestKeys,
  vmGpus,
  localRootGb,
  type ProfileSize,
} from '../lib/gpu';
import { describeProbes, probesFor } from '../lib/probes';
import { guestOsOf, identifyGuestOs } from '../lib/guestOs';
import { kitVmName } from '../lib/kitName';
import { addressRefusal, canonicalIp, ipInCidr, mappedNetwork, nicOverride, nicTarget, noHostAddress } from '../lib/nics';
import { kitSubnet, networkList, portRefusal, resolveNetwork, subnetsOf } from '../lib/networks';
import { destinationRelease, parseMicroversion, type PolicyProfile } from '../lib/platforms';
import { describePorts, portsWithoutOneFixedIp } from '../lib/readiness';
import { resolveDestination, splitHost, storageBackends } from '../lib/storage';
import { providerLabel } from '../lib/providerUse';
import { buildStrategyEvidence } from './mockEvidence';
import { FINDING_CATALOG } from './findingCatalog';
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
    // why the answer could not be applied - a fallback's reason -, as the API records it (SDD §4.2, audit AD-03)
    recommendation: { option, confidence, distribution, unusable: status === 'fallback_rules' ? (input.fallback ?? null) : null },
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
  return { status: d.status, final_option: d.final_option, reason_code: d.reason_code, reason_text: d.reason_text, policy_id: d.policy_id, threshold_used: d.threshold_used };
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
  cold: 'Stop the VM, copy every disk, boot it on the destination: downtime grows with the used data.',
  warm: 'Copy snapshots while the VM runs, then stop it and sync only changed blocks.',
  storage_handover: 'Hand the volumes over in the shared storage without copying: a metadata-only cutover.',
  vmware_cold: 'Stop the VMware VM and convert every disk with virt-v2v.',
  vmware_warm: 'Copy with CBT while the VMware VM runs, then a final delta after shutdown.',
};

const SERIES_NAME = /^\d{4}\.[12]$/;

/**
 * The destination by kind and series, as the API names it to Jev (`destination_descriptor`; SDD §1 naming, §13.4,
 * review RV20-01): "RHOSO 18.0", the community series the check settled - never the provider's name, which is operator
 * free text. A check's report says what is settled, the declared series too unless the APIs contradict it, so the
 * declared series alone counts only before any check reported one (review RV21-09).
 */
export function destinationDescriptor(destination: Pick<Provider, 'kind' | 'capabilities' | 'release'>): string {
  if (destination.kind === 'rhoso') return 'RHOSO 18.0';
  const series = 'release' in destination.capabilities ? destination.capabilities.release : destination.release;
  return typeof series === 'string' && SERIES_NAME.test(series) ? `a community OpenStack ${series} destination` : 'a community OpenStack destination';
}

/**
 * The API's `replace_names` (SDD §13.4): each name replaced wherever it stands as a word - between characters that
 * are not letters or digits, so web-01-data loses web-01 while web-010 keeps it - in one pass over every name, the
 * longest first where two start at the same place, so what a replacement wrote is never searched again (review RV22-04).
 */
export function replaceNames(text: string, replacements: Record<string, string>): string {
  const names = Object.keys(replacements)
    .filter(Boolean)
    .sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0));
  if (!names.length) return text;
  const words = new RegExp(`(?<![\\p{L}\\p{N}])(?:${names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})(?![\\p{L}\\p{N}])`, 'gu');
  return text.replace(words, (found) => replacements[found]!);
}

/** The advisor's input for a strategy choice, as the API snapshots it (SDD §14.5): the jev_decide arguments and the scope. */
export function strategySnapshot(input: {
  vm: VMRef;
  plan: Plan;
  estimates: Estimate[];
  findings: Finding[];
  scope: readonly string[];
  deterministic: Strategy;
  at: string;
  /** The plan's destination by kind and series (`destinationDescriptor`): the question names it (SDD §1 naming). */
  destination?: string;
  /** Its provider's own name: replaced by the descriptor in the question and the evidence, never sent (SDD §13.4). */
  destinationName?: string;
}): AdvisorSnapshot {
  const { vm, plan, estimates, findings } = input;
  const where = input.destination ?? 'the destination';
  const withoutProvider = (text: string) => (input.destinationName ? replaceNames(text, { [input.destinationName]: where }) : text);
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
      // the VM's name alone, never the descriptor the question holds (review RV21-10)
      decision: `Which migration strategy should be used for VM ${withoutProvider(vm.name)} moving to ${where}?`,
      evidence: withoutProvider(
        `VM ${vm.name}: ${vm.vcpus} vCPU, ${vm.ram_mb} MB RAM, ${vm.disks.length} disk(s) [${disks}], provisioned ${gib(vm.disk_bytes)}, used ${gib(vm.used_bytes)}. ` +
          `Guest write rate ${rate}. Downtime SLO ${plan.downtime_slo_s} s. Link ${Math.round(plan.link_bps / MiB)} MiB/s. ` +
          `Findings: ${findings.map((f) => `${f.code} (${f.severity})`).join(', ') || 'none'}. Estimates: ${estimateText}.`,
      ),
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
  gpus?: GpuDevice[];
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

/** The demo clusters' fsids (SDD §10, §28.4, plan task CE11): an rbd pool reports its cluster's and its Ceph pool. */
export const DEMO_CEPH_FSID = '6f8c2d4e-1b3a-4c5d-9e7f-0a1b2c3d4e5f';
export const DEMO_PROD_CEPH_FSID = 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';

const backend = (pool: string, family: 'rbd' | 'netapp_nfs' | 'other', ceph?: { fsid: string; pool: string }) => ({
  pool,
  vendor: family === 'netapp_nfs' ? 'NetApp' : 'Open Source',
  protocol: family === 'netapp_nfs' ? 'nfs' : family === 'rbd' ? 'ceph' : 'iSCSI',
  family,
  // an rbd entry alone carries them, as the API's check (SDD §10): null without location_info
  ...(family === 'rbd' ? { ceph_fsid: ceph?.fsid ?? null, ceph_pool: ceph?.pool ?? null } : {}),
});

const DC1_STORAGE = [
  backend('overcloud@tripleo_ceph#ceph-hdd', 'rbd', { fsid: DEMO_CEPH_FSID, pool: 'volumes' }),
  backend('overcloud@tripleo_ceph#ceph-ssd', 'rbd', { fsid: DEMO_CEPH_FSID, pool: 'volumes-ssd' }),
  backend('overcloud@tripleo_ceph#tripleo-ceph', 'rbd', { fsid: DEMO_CEPH_FSID, pool: 'tripleo-ceph' }),
  backend('overcloud@tripleo_ceph#tripleo-ceph-ssd', 'rbd', { fsid: DEMO_CEPH_FSID, pool: 'tripleo-ceph-ssd' }),
  backend('overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1', 'netapp_nfs'),
];

const PROD_STORAGE = [
  backend('hostgroup@ceph-hdd#volumes-hdd', 'rbd', { fsid: DEMO_PROD_CEPH_FSID, pool: 'volumes-hdd' }),
  backend('hostgroup@ceph-nvme#volumes-nvme', 'rbd', { fsid: DEMO_PROD_CEPH_FSID, pool: 'volumes-nvme' }),
  backend('hostgroup@ceph-ssd#volumes-ssd', 'rbd', { fsid: DEMO_PROD_CEPH_FSID, pool: 'volumes-ssd' }),
  // the same ONTAP export, mounted through the RHOSO storage network's LIF
  backend('hostgroup@ontap-nfs#198.51.100.60:/cinder_dc1', 'netapp_nfs'),
];

const DC3_STORAGE = [backend('kolla@rbd-1#volumes', 'rbd')];

/** The maxima the demo's clouds report, as the API's fakes do (FakeSourceProvider; FakeDestinationProvider by kind). */
const MAXIMA = { source: ['2.88', '3.64'], rhoso: ['2.95', '3.70'], openstack: ['2.104', '3.71'] } as const;

/** The demo's admin token's role names: its check report and its destinations' inventories name them (SDD §10). */
export const DEMO_TOKEN_ROLES: readonly string[] = ['admin', 'member', 'reader'];

/** The projects the demo's provider credentials are scoped to, as the API's fakes name them in their check (SDD §10, §27.7). */
const DEMO_OWN_PROJECTS = {
  source: { id: 'project-finance', name: 'finance', domain: 'Default' },
  destination: { id: 'project-demo', name: 'demo', domain: 'Default' },
} as const;

/** A Keystone project of a mock cloud that a project credential may be scoped to (SDD §27.4), with what its check reads. */
export interface MockCloudProject {
  name: string;
  domain: string;
  /** The user its application credential acts as: a user per project credential (§27.6, owner decision 3). */
  user_id: string;
  roles: string[];
  restricted: boolean;
  /** Milliseconds from the mock's clock to its application credential's expiry; null when it never expires. */
  expires_in: number | null;
}

/**
 * The Keystone projects each mock cloud has beside its credential's own (SDD §27.4, plan task TC8), by project id - 32
 * hex digits, as Keystone makes them. A project credential the mock is given for any other id fails its check, as one
 * Keystone refuses does (§27.7); the inventory of a project lists the source's VMs of that name (§10, §12).
 */
export const MOCK_CLOUD_PROJECTS: Readonly<Record<string, Readonly<Record<string, MockCloudProject>>>> = {
  'rhosp17-dc1': {
    '5f0c3a9e2b7d41c8a6e9d2f1b3c4a5e6': { name: 'shop', domain: 'Default', user_id: '0b6d2a4f8e1c43579a2d6f0e8b4c1a37', roles: ['member', 'reader'], restricted: true, expires_in: 60 * 24 * 3600_000 },
    '8d2e6b1f4a9c47e3b5d0c8a7f6e1d2c3': { name: 'platform', domain: 'Default', user_id: '7e3a9c1d5b2f48e6a0c4d8b2f6e1a953', roles: ['load-balancer_member', 'member', 'reader'], restricted: false, expires_in: 5 * 24 * 3600_000 },
    c41a7e9b3d2f48a6b1e5c9d0f7a3b2e8: { name: 'analytics', domain: 'Default', user_id: '2c8f4e6a0b1d47c39e5a7d3b1f8c6e24', roles: ['member', 'reader'], restricted: true, expires_in: null },
  },
};

/** The project credentials the mock seeds (SDD §16, plan task TC8): the RHOSP 17.1 source's for shop and platform. */
const SEEDED_PROJECT_CREDENTIALS: Readonly<Record<string, readonly string[]>> = {
  'rhosp17-dc1': ['5f0c3a9e2b7d41c8a6e9d2f1b3c4a5e6', '8d2e6b1f4a9c47e3b5d0c8a7f6e1d2c3'],
};

/** A project credential's secret name, `provider-{id}.project-<h>`: the first 16 hex digits of its project id's SHA-256 (SDD §13.3). */
export function projectSecretName(providerId: string, projectId: string): string {
  return `provider-${providerId}.project-${sha256Hex(projectId).slice(0, 16)}`;
}

/**
 * A project credential's entry of its provider's check report at `checkedAt` (SDD §10, §27.7): what its token and record
 * read on the mock cloud, or an error with nothing read for a project the cloud does not have.
 */
export function projectCheckOf(providerId: string, projectId: string, checkedAt: string): ProjectCheck {
  const project = MOCK_CLOUD_PROJECTS[providerId]?.[projectId];
  if (!project) {
    return { status: 'error', message: `${providerId}: Keystone refused the application credential (HTTP 401)`, project_name: null, domain: null, user_id: null, roles: null, restricted: null, expires_at: null, checked_at: checkedAt };
  }
  const expires = project.expires_in === null ? null : new Date(Date.parse(checkedAt) + project.expires_in).toISOString();
  return { status: 'ok', message: null, project_name: project.name, domain: project.domain, user_id: project.user_id, roles: [...project.roles], restricted: project.restricted, expires_at: expires, checked_at: checkedAt };
}

/** The check report's `projects` of the provider's credentials at `checkedAt`, by project id (SDD §10, §27.7). */
export function projectsReport(providerId: string, projectIds: readonly string[], checkedAt: string): Record<string, ProjectCheck> {
  return Object.fromEntries([...projectIds].sort().map((id) => [id, projectCheckOf(providerId, id, checkedAt)]));
}

/** The vGPU profiles the demo's GPU destinations declare for their two vGPU flavors (`gpu_profiles`, SDD §22.3, §22.10). */
const DEMO_GPU_PROFILES: Readonly<Record<string, string>> = { 'g1.a100-4c': 'A100-4C', 'g1.a100-3-20c': 'A100-3-20C' };

/** The GPU flavors of the demo's GPU destinations (SDD §22.10): two vGPU types by their traits, and an A100 by PCI alias. */
const DEMO_GPU_FLAVORS: DestinationFlavor[] = [
  { name: 'g1.a100-4c', vcpus: 8, ram_mb: 32768, disk_gb: 200, extra_specs: { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' } },
  { name: 'g1.a100-3-20c', vcpus: 16, ram_mb: 65536, disk_gb: 300, extra_specs: { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_3_20C': 'required' } },
  { name: 'p1.a100', vcpus: 16, ram_mb: 131072, disk_gb: 400, extra_specs: { 'pci_passthrough:alias': 'a100:1' } },
];

/**
 * The pools the Placement of the demo's GPU destinations holds for those flavors, as §22.3 reads them (SDD §22.10): the
 * A100-4C type's 4 units free on one GPU, the MIG-backed A100-3-20C type's 2 on another, and the class alone, which
 * unites them. Each pool its own object: a destination's GPUs are its own.
 */
function demoGpuPools(): GpuPool[] {
  const pool = (traits: string[], free: number, providers: number, largest: number): GpuPool => ({
    resource_class: 'VGPU',
    traits,
    forbidden_traits: [],
    same_provider: false,
    free,
    providers,
    largest_free: largest,
    detail: null,
  });
  return [pool(['CUSTOM_NVIDIA_A100_4C'], 4, 1, 4), pool(['CUSTOM_NVIDIA_A100_3_20C'], 2, 1, 2), pool([], 6, 2, 4)];
}

/**
 * A destination's flavors that ask for a GPU, with the profile its provider declares for each, as the API's inventory
 * lists them (`gpu_flavors`, SDD §22.3): their requests read by `gpu_requests`.
 */
export function gpuFlavorsOf(flavors: readonly DestinationFlavor[], profiles: Readonly<Record<string, string>> = {}): GpuFlavor[] {
  return flavors.flatMap(({ name, vcpus, ram_mb, disk_gb, extra_specs }) => {
    const gpus = gpuRequests(extra_specs, Object.hasOwn(profiles, name) ? profiles[name]! : null);
    return gpus.length ? [{ name, vcpus, ram_mb, disk_gb, gpus }] : [];
  });
}

/**
 * A destination's GPU flavors and GPU capacity as a read at `readAt` finds them, its inventory's, its check's and its
 * validation's alike, as the API reads them (SDD §10, §22.3): the flavors whose extra specs ask for a GPU, with the
 * profiles its provider declares at the time of the read (`gpu_profiles`), and the pools of their vGPU requests as the
 * destination's Placement holds them (`gpu_capacity`), each a pool of its own - a pool no GPU holds has none free.
 * Without a vGPU request Placement is sent nothing; a read Placement refused or failed has no pool.
 */
export function readDestinationGpus(
  inventory: DestinationInventory | VMRef[] | undefined,
  profiles: Readonly<Record<string, string>> | undefined,
  readAt: string,
): { gpu_flavors: GpuFlavor[]; gpu_capacity: GpuCapacity } {
  const held = inventory && !Array.isArray(inventory) ? inventory : undefined;
  const gpuFlavors = gpuFlavorsOf(held?.flavors ?? [], profiles ?? {});
  const asked = gpuPools(gpuFlavors.flatMap((f) => f.gpus));
  const placement = held?.gpu_capacity;
  const ok = (pools: GpuPool[]): GpuCapacity => ({ status: 'ok', reason: null, detail: null, read_at: readAt, pools });
  if (!asked.length) return { gpu_flavors: gpuFlavors, gpu_capacity: ok([]) };
  if (placement && placement.status !== 'ok') return { gpu_flavors: gpuFlavors, gpu_capacity: { ...placement, read_at: readAt, pools: [] } };
  const pools = asked.map((pool) => {
    const found = placement?.pools.find((p) => samePool(p, pool));
    const counts = found ? { free: found.free, providers: found.providers, largest_free: found.largest_free, detail: found.detail } : { free: 0, providers: 0, largest_free: 0, detail: null };
    return { ...pool, traits: [...pool.traits], forbidden_traits: [...pool.forbidden_traits], ...counts };
  });
  return { gpu_flavors: gpuFlavors, gpu_capacity: ok(pools) };
}

/**
 * An OpenStack or RHOSO provider's check report, with the keys the API's check reports (SDD §10; check_report_keys.json):
 * its token's roles and project, its services' maxima, its storage pools and TLS; a destination's adds its series, support tier and
 * policy profile, by its maxima and what it declares (§10.1), and its GPU capacity, read at `checkedAt` for the GPU
 * flavors of its `inventory` with the profiles it declares (§22.3).
 */
function openstackCheck(
  provider: Pick<Provider, 'kind' | 'role' | 'release' | 'policy_profile' | 'gpu_profiles'>,
  storage: ReturnType<typeof backend>[],
  checkedAt: string,
  extra: Record<string, unknown> = {},
  inventory?: DestinationInventory | VMRef[],
): Record<string, unknown> {
  const [compute, blockStorage] = provider.role === 'source' ? MAXIMA.source : MAXIMA[provider.kind === 'rhoso' ? 'rhoso' : 'openstack'];
  return {
    admin: true,
    // an admin token (SDD §10): the demo's shared provider network takes its ports (§6.6)
    roles: [...DEMO_TOKEN_ROLES],
    // the project its token is scoped to (SDD §10, §27.7)
    project: { ...DEMO_OWN_PROJECTS[provider.role] },
    compute_microversion: compute,
    block_storage_microversion: blockStorage,
    ovn: true,
    volume_backends: storage.map((b) => b.pool),
    storage_backends: storage,
    tls: true,
    plain_http: [],
    ...(provider.role === 'destination'
      ? {
          ...destinationRelease(provider.kind, provider.release, provider.policy_profile, compute, blockStorage),
          gpu_capacity: readDestinationGpus(inventory, provider.gpu_profiles, checkedAt).gpu_capacity,
        }
      : {}),
    ...extra,
  };
}

/** The storage pools a provider the demo did not seed lists when the mock's check reads it (SDD §7.3.1). */
export const CHECKED_STORAGE = [backend('hostgroup@ceph-ssd#ceph-ssd', 'rbd')];

/**
 * What the mock's check reads from each seeded cloud: its pools, and any key it reports otherwise. The seed and every
 * later check report the same, as the API demo's fakes do, so a report a reset cleared reads back unchanged (SDD §10, §12;
 * review RV19-02).
 */
const CLOUDS: Record<string, { role: ProviderRole; kind: ProviderKind; storage: ReturnType<typeof backend>[]; extra?: Record<string, unknown> }> = {
  'rhosp17-dc1': { role: 'source', kind: 'openstack', storage: DC1_STORAGE, extra: { ovn: false } },
  'rhoso-prod': { role: 'destination', kind: 'rhoso', storage: PROD_STORAGE },
  'hibiscus-dc3': { role: 'destination', kind: 'openstack', storage: DC3_STORAGE },
};

/**
 * A vCenter's check report, with the keys the API demo's VMware check reports (FakeSourceProvider): its VMs counted, and
 * no OpenStack service - no Nova, OVN or Cinder (SDD §10; review RV19-05).
 */
export function vmwareCheck(vms: readonly VMRef[]): Record<string, unknown> {
  return {
    api_version: '8.0.2.0',
    version: '8.0.2',
    vm_count: vms.length,
    cbt_enabled_vms: vms.filter((v) => v.cbt_enabled).length,
    vms_with_snapshots: vms.filter((v) => v.snapshot_count).length,
    vms_with_independent_disks: vms.filter((v) => v.disks.some((d) => d.independent)).length,
    vms_without_tools: vms.filter((v) => v.tools_ok === false).length,
  };
}

/**
 * An OpenStack or RHOSO provider's check report, as the mock's check reads its cloud at `checkedAt`: the seeded cloud's
 * own pools, else CHECKED_STORAGE, and a destination's GPU capacity for the GPU flavors of its `inventory` (SDD §22.3).
 */
export function checkOf(
  provider: Pick<Provider, 'id' | 'kind' | 'role' | 'release' | 'policy_profile' | 'gpu_profiles'>,
  checkedAt: string,
  inventory?: DestinationInventory | VMRef[],
): Record<string, unknown> {
  // a seeded cloud's own pools only for a provider of its role and kind: one re-created under its id as another
  // provider reads the generic pools, never the deleted cloud's (review RV20-14)
  const seeded = CLOUDS[provider.id];
  const cloud = seeded && seeded.role === provider.role && seeded.kind === provider.kind ? seeded : undefined;
  return openstackCheck(provider, cloud?.storage ?? CHECKED_STORAGE, checkedAt, cloud?.extra, inventory);
}

/** Each device by what names it - a profile, an alias, the PCI ids or a vGPU's class -, with its count when more than one: the API's _gpu_names (SDD §9.3). */
function gpuNames(devices: GpuDevice[]): string {
  return devices
    .map((d) => {
      const ids = d.vendor_id && d.device_id ? `${d.vendor_id}:${d.device_id}` : '';
      const name = d.profile || d.alias || ids || d.resource_class || 'a device without ids';
      return d.count > 1 ? `${name} ×${d.count}` : name;
    })
    .join(', ');
}

/**
 * The destination's flavors by name, those of them that ask for a GPU with the profiles its provider declares, and its GPU
 * capacity, as a read at `readAt` finds them (`readDestinationGpus`): what the pre-flight's GPU mapping rule and its GPU
 * findings read (SDD §22.3, §22.4, §22.5).
 */
export function flavorContext(
  inventory: DestinationInventory | VMRef[] | undefined,
  profiles: Readonly<Record<string, string>> | undefined,
  readAt: string,
): { flavors: string[]; gpuFlavors: GpuFlavor[]; gpuCapacity: GpuCapacity } {
  const { gpu_flavors, gpu_capacity } = readDestinationGpus(inventory, profiles, readAt);
  const flavors = inventory && !Array.isArray(inventory) ? inventory.flavors.map((f) => f.name) : [];
  return { flavors, gpuFlavors: gpu_flavors, gpuCapacity: gpu_capacity };
}

/** What a cutover of a VM will still create at the destination: all of it, its server alone, or nothing (SDD §9.3). */
export type QuotaShare = 'all' | 'server' | 'nothing';
/** The phases whose migration has created its destination volumes, or is creating them: its server is still to come. */
const SERVER_STILL_TO_COME: ReadonlySet<Phase> = new Set(['precopy', 'syncing', 'awaiting_cutover', 'cutover']);
/** The phases whose migration holds what it created, or is releasing it: in use, so already off the free quota. */
const NOTHING_STILL_TO_COME: ReadonlySet<Phase> = new Set(['verifying', 'completed', 'finalized', 'rolling_back', 'cancelled']);

/**
 * What a cutover of a VM whose migration is in `phase` (null: no migration yet) will still create at the destination, the
 * API's quota_share (SDD §9.3, reviews PL-02, RV38A-01): nothing once what it holds is in use, or being released; its server
 * alone while its destination volumes exist or are being created, and when it holds those an earlier attempt created.
 */
export function quotaShare(phase: Phase | null, holdsVolumes = false): QuotaShare {
  if (phase !== null && NOTHING_STILL_TO_COME.has(phase)) return 'nothing';
  return (phase !== null && SERVER_STILL_TO_COME.has(phase)) || holdsVolumes ? 'server' : 'all';
}

/**
 * Whether a GPU VM's mapping applies, in the API's words (_gpu_mapping, SDD §22.4): its GPU flavor when it does; why not
 * - a VMware device without a key, a key no GPU mapping names, keys mapping to different flavors, an OpenStack flavor no
 * mapping names, a mapping to a flavor that asks for no GPU -; neither for a mapped flavor the destination lacks, which is
 * MAP_FLAVOR_MISSING's.
 */
function gpuMapping(
  vm: VMRef,
  gpus: GpuDevice[],
  sourceKind: ProviderKind,
  plan: Plan,
  flavors: readonly string[],
  gpuFlavors: readonly GpuFlavor[],
  where: string,
): { flavor: GpuFlavor | null; why: string | null } {
  const why = (text: string) => ({ flavor: null, why: text });
  let target: string | undefined;
  let mapping: string;
  if (sourceKind === 'vmware') {
    const keys = [...new Set(gpus.map(gpuKey))];
    if (keys.includes(null)) return why('No mapping can name its device without ids, so none applies to the VM.');
    const named = keys as string[];
    const mapped = plan.mappings.gpus ?? {};
    const unnamed = named.filter((key) => !Object.hasOwn(mapped, key));
    if (unnamed.length)
      return why(`The plan's GPU mappings name no flavor for ${unnamed.map(pyRepr).join(', ')}: map ${unnamed.length === 1 ? 'it' : 'them'} to a GPU flavor of ${where}.`);
    const targets = named.map((key) => [key, mapped[key]!] as const);
    if (new Set(targets.map(([, flavor]) => flavor)).size > 1)
      return why(`Its GPUs map to different flavors (${targets.map(([key, flavor]) => `${pyRepr(key)} to ${pyRepr(flavor)}`).join(', ')}): its server is created with one.`);
    target = targets[0]![1];
    mapping = `Its GPU mapping, ${named.map(pyRepr).join(', ')} to ${pyRepr(target)},`;
  } else {
    const flavor = vm.flavor ? pyRepr(vm.flavor) : 'None';
    target = vm.flavor && Object.hasOwn(plan.mappings.flavors, vm.flavor) ? plan.mappings.flavors[vm.flavor] : undefined;
    if (target === undefined) return why(`The plan maps no flavor to ${flavor}: map it to a GPU flavor of ${where}.`);
    mapping = `Its flavor mapping, ${flavor} to ${pyRepr(target)},`;
  }
  if (!flavors.includes(target)) return { flavor: null, why: null };
  const gpuFlavor = gpuFlavors.find((f) => f.name === target);
  return gpuFlavor ? { flavor: gpuFlavor, why: null } : why(`${mapping} names a flavor of ${where} that asks for no GPU: the server would lose its GPU.`);
}

/**
 * The mappings of a GPU VM whose flavor the destination lacks, in the API's words (_missing_gpu_targets, SDD §9.3
 * MAP_FLAVOR_MISSING, §22.4): its flavor mapping, or for a VMware VM each GPU key's.
 */
function gpuMissingTargets(vm: VMRef, gpus: GpuDevice[], sourceKind: ProviderKind, plan: Plan, flavors: readonly string[], where: string): string[] {
  if (sourceKind !== 'vmware') {
    const target = vm.flavor && Object.hasOwn(plan.mappings.flavors, vm.flavor) ? plan.mappings.flavors[vm.flavor] : undefined;
    return target === undefined || flavors.includes(target)
      ? []
      : [`Flavor ${pyRepr(vm.flavor!)} is mapped to ${pyRepr(target)}, which does not exist in ${where}.`];
  }
  const mapped = plan.mappings.gpus ?? {};
  const keys = [...new Set(gpus.map(gpuKey).filter((key): key is string => key !== null))];
  return keys
    .filter((key) => Object.hasOwn(mapped, key) && !flavors.includes(mapped[key]!))
    .map((key) => `The plan maps ${pyRepr(key)} to ${pyRepr(mapped[key]!)}, which does not exist in ${where}.`);
}

/** A finding the GPU rules give: its code, its message, and what it has of its own over its catalog entry (SDD §9.3). */
type GpuFinding = [code: string, message: string, own?: { severity?: Severity; remediation?: string }];

/** A GPU flavor's requests in words, the API's _requests_text: "vGPU VGPU CUSTOM_NVIDIA_A100_4C (A100-4C)" (SDD §22.5). */
function requestsText(requests: readonly GpuDevice[]): string {
  return requests
    .map((d) => {
      const what = d.kind === 'vgpu' ? `vGPU ${poolName(d.resource_class ?? '', d.traits, d.forbidden_traits)}` : `PCI alias ${d.alias ?? 'None'}`;
      return what + (d.count > 1 ? ` ×${d.count}` : '') + (d.profile ? ` (${d.profile})` : '');
    })
    .join(', ');
}

/** "1 unit", "2 units": the API's _count. */
function counted(n: number, noun: string): string {
  return n === 1 ? `${n} ${noun}` : `${n} ${noun}s`;
}

/** The names sorted, the first `limit` of them, then how many more, the API's _first_names (SDD §22.5). */
function firstNames(names: Iterable<string>, limit = 10): string {
  const ordered = [...new Set(names)].sort(byCodePoint);
  return ordered.length <= limit ? ordered.join(', ') : `${ordered.slice(0, limit).join(', ')} and ${ordered.length - limit} more`;
}

/** "a", "a and b", "a, b and c": the API's _and. */
function joinedAnd(items: readonly string[]): string {
  return items.length === 1 ? items[0]! : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
}

/** Each GPU unit whose profile has a size, with the profile, the largest first: the API's _sized (SDD §22.2). */
function sized(devices: readonly GpuDevice[]): Array<[ProfileSize, string]> {
  const units: Array<[ProfileSize, string]> = [];
  for (const d of devices) {
    const size = d.profile ? vgpuProfileSize(d.profile) : null;
    if (size) for (let i = 0; i < d.count; i += 1) units.push([size, d.profile!]);
  }
  // Python's sorted(reverse=True) keeps equal units in their order, as a stable descending sort does
  return units.sort(([a], [b]) => b.framebuffer_gb - a.framebuffer_gb || (b.slices ?? 0) - (a.slices ?? 0));
}

/** Whether every passed-through device is an NVIDIA GPU - a VMware device of vendor 10de -, the API's _nvidia_passthrough:
 * an OpenStack PCI alias names no device, so it is "a PCI device" (SDD §22.1). */
function nvidiaPassthrough(devices: readonly GpuDevice[]): boolean {
  const passthrough = devices.filter((d) => d.kind === 'passthrough');
  return passthrough.length > 0 && passthrough.every((d) => d.vendor_id === '10de');
}

/** A vGPU among them: one of kind vgpu, or a PCI device with a vGPU profile (SDD §22.3), the API's _is_vgpu. */
function isVgpu(devices: readonly GpuDevice[]): boolean {
  return devices.some((d) => d.kind === 'vgpu' || (d.kind === 'passthrough' && Boolean(d.profile)));
}

/** What the guest gets, in words, the API's _gpu_words: a vGPU, a passed-through NVIDIA GPU or a PCI device. */
function gpuWords(devices: readonly GpuDevice[]): string {
  if (isVgpu(devices)) return 'a vGPU';
  return nvidiaPassthrough(devices) ? 'a passed-through NVIDIA GPU' : 'a passed-through PCI device';
}

/** MAP_GPU_FLAVOR and MAP_GPU_DOWNSIZED for a GPU mapping that applies, in the API's words (_gpu_flavor_findings, SDD §22.5). */
function gpuFlavorFindings(vm: VMRef, gpus: readonly GpuDevice[], flavor: GpuFlavor, where: string): GpuFinding[] {
  const vcpus = flavor.vcpus || 0;
  const ramMb = flavor.ram_mb || 0;
  const diskGb = flavor.disk_gb || 0;
  const root = localRootGb(vm);
  const less = [
    vcpus < vm.vcpus ? `${vcpus} vCPUs for its ${vm.vcpus}` : null,
    ramMb < vm.ram_mb ? `${ramMb} MB RAM for its ${vm.ram_mb}` : null,
    diskGb < root ? `${diskGb} GB of root disk for its ${root}` : null,
  ].filter((text): text is string => text !== null);
  const out: GpuFinding[] = [
    [
      'MAP_GPU_FLAVOR',
      `The VM's GPUs (${gpuNames([...gpus])}) go to flavor ${pyRepr(flavor.name)} of ${where}: ${vcpus} vCPUs, ${ramMb} MB RAM, ${diskGb} GB disk, ${requestsText(flavor.gpus)}` +
        (less.length ? `; it gives the VM ${joinedAnd(less)}` : '') +
        '. GPU migration is a Technology Preview.',
      // the GPU flavor decides the server's size, one per GPU key of a plan (SDD §22.4)
      less.length
        ? {
            severity: 'warning',
            remediation:
              "Map the VM's GPU to a GPU flavor of the destination of at least its size, or put the VMs that need another size in another plan, which maps their GPU to another flavor.",
          }
        : undefined,
    ],
  ];
  const has = gpus.reduce((sum, d) => sum + d.count, 0);
  const gives = flavor.gpus.reduce((sum, d) => sum + d.count, 0);
  const reasons = gives < has ? [`${gives} GPU${gives === 1 ? '' : 's'} for its ${has}`] : [];
  // compared one by one, the largest first: as many pairs as both sides have sizes for
  const own = sized(gpus);
  const offered = sized(flavor.gpus);
  const pairs = own.slice(0, Math.min(own.length, offered.length)).map((unit, i) => [unit, offered[i]!] as const);
  const smaller = pairs.find(([v, f]) => f[0].framebuffer_gb < v[0].framebuffer_gb);
  if (smaller) {
    const [[mine, mineName], [other, otherName]] = smaller;
    reasons.push(`a ${other.framebuffer_gb} GB framebuffer for its ${mine.framebuffer_gb} GB (${otherName} for ${mineName})`);
  }
  const fewer = pairs.find(([v, f]) => v[0].slices !== null && f[0].slices !== null && f[0].slices < v[0].slices);
  if (fewer) {
    const [[mine, mineName], [other, otherName]] = fewer;
    reasons.push(`${counted(other.slices!, 'MIG slice')} for its ${mine.slices} (${otherName} for ${mineName})`);
  }
  if (reasons.length) out.push(['MAP_GPU_DOWNSIZED', `Flavor ${pyRepr(flavor.name)} of ${where} gives the VM less GPU than it has: ${reasons.join('; ')}.`]);
  return out;
}

/**
 * GUEST_GPU_DRIVER for every GPU VM, and GUEST_GPU_LICENSE for a vGPU's, in the API's words (_guest_gpu_findings, SDD
 * §22.5): the driver the GPU it gets needs - its GPU flavor's when a mapping applies, else its own -, and the change of
 * kind or model the mapping makes.
 */
function guestGpuFindings(gpus: readonly GpuDevice[], flavor: GpuFlavor | null, sourceKind: ProviderKind, where: string): GpuFinding[] {
  const target = flavor ? flavor.gpus : gpus;
  const toVgpu = isVgpu(target);
  let message = toVgpu
    ? `The guest's NVIDIA driver must suit ${where}: a vGPU guest driver of a release its hosts' vGPU manager supports (NVIDIA's compatibility table).`
    : nvidiaPassthrough(target)
      ? `The guest's NVIDIA driver must suit ${where}: the data center driver for its passed-through NVIDIA GPU.`
      : `The guest's driver must suit ${where}: the driver of its passed-through PCI device.`;
  if (flavor) {
    const models = [...new Set(sized(gpus).map(([size]) => size.gpu))].sort(byCodePoint);
    const newModels = [...new Set(sized(target).map(([size]) => size.gpu))].sort(byCodePoint);
    if (isVgpu(gpus) !== toVgpu) message += ` The mapping changes the GPU from ${gpuWords(gpus)} to ${gpuWords(target)}, so the guest needs a new driver.`;
    else if (models.length && newModels.length && models.join('\n') !== newModels.join('\n'))
      message += ` The mapping changes the GPU model from ${models.join(', ')} to ${newModels.join(', ')}, so the guest needs a new driver.`;
  }
  if (sourceKind === 'vmware') message += ' virt-v2v neither installs nor updates the driver.';
  const out: GpuFinding[] = [['GUEST_GPU_DRIVER', message]];
  if (isVgpu(gpus) || toVgpu)
    out.push([
      'GUEST_GPU_LICENSE',
      `The guest licenses its vGPU from the NVIDIA License System: its license server, a CLS instance or an on-premises DLS instance, must be reachable from its network in ${where}, or the guest runs its GPU unlicensed.`,
    ]);
  return out;
}

/**
 * The GPU flavor among `gpuFlavors` that a VM's server is created with, the API's server_gpu_flavor (SDD §22.5, §22.7): a
 * GPU VM's GPU flavor, or the GPU flavor a flavor mapping sends a VM without a GPU to.
 */
export function serverGpuFlavor(
  vm: Pick<VMRef, 'gpus' | 'flavor_extra_specs' | 'flavor'>,
  plan: Pick<Plan, 'mappings'>,
  gpuFlavors: readonly GpuFlavor[],
  sourceKind: ProviderKind,
): GpuFlavor | null {
  const name = vmGpus(vm).length
    ? gpuFlavorOf(vm, plan, sourceKind)
    : vm.flavor && Object.hasOwn(plan.mappings.flavors, vm.flavor)
      ? plan.mappings.flavors[vm.flavor]!
      : null;
  return name ? (gpuFlavors.find((f) => f.name === name) ?? null) : null;
}

/** Why the destination's GPU capacity cannot be read, in the API's words (_unknown_capacity, SDD §22.3, §22.5). */
function unknownCapacity(capacity: GpuCapacity): string {
  if (capacity.reason === 'forbidden') return `Placement refuses its credential (${capacity.detail ?? 'None'}): its default policy lets only the admin role read it`;
  if (capacity.reason === 'no_placement') return 'its catalog has no Placement';
  return `the read failed (${capacity.detail ?? 'None'})`;
}

/**
 * DST_GPU_CAPACITY_UNKNOWN and DST_GPU_CAPACITY_INSUFFICIENT for a VM whose server is created with a GPU flavor, in the
 * API's words (_capacity_findings, SDD §22.5): the plan's VMs each count what their cutover will still create, per pool and
 * per resource class across its pools; a group of n > 1 units needs a provider with n free; a pool Placement refused
 * counts nothing. Nothing is guessed.
 */
function capacityFindings(
  vm: VMRef,
  flavor: GpuFlavor,
  plan: Plan,
  gpuFlavors: readonly GpuFlavor[],
  capacity: GpuCapacity,
  sourceKind: ProviderKind,
  where: string,
  selected: readonly VMRef[],
  shares: Readonly<Record<string, QuotaShare>>,
): GpuFinding[] {
  const share = (vmId: string) => (Object.hasOwn(shares, vmId) ? shares[vmId] : undefined);
  const vgpus = flavor.gpus.filter((g) => g.kind === 'vgpu' && g.resource_class);
  const read = new Set(capacity.pools.map((p) => requestPoolKey(p)));
  const unknown: string[] = [];
  if (vgpus.length && capacity.status === 'unknown') unknown.push(`The GPU capacity of ${where} cannot be read: ${unknownCapacity(capacity)}.`);
  else if (vgpus.length) {
    // a read that holds no pool for a request: nothing is guessed for it (SDD §22.5)
    for (const g of vgpus)
      if (!read.has(requestPoolKey(g)))
        unknown.push(`The GPU capacity read of ${where} holds no pool ${poolName(g.resource_class ?? '', g.traits, g.forbidden_traits)}: nothing is guessed for it.`);
  }
  for (const g of flavor.gpus)
    if (g.kind === 'passthrough')
      unknown.push(`Flavor ${pyRepr(flavor.name)} of ${where} asks for PCI alias ${g.alias ?? 'None'}, whose devices nova.conf configures: Placement does not show their capacity.`);
  const out: GpuFinding[] = unknown.length ? [['DST_GPU_CAPACITY_UNKNOWN', unknown.join(' ')]] : [];
  if (!vgpus.length || capacity.status !== 'ok' || share(vm.source_id) === 'nothing') return out;
  // the units each pool, and each class across its pools, is asked for by the plan's VMs
  const asked = new Map<string, number>();
  const askers = new Map<string, Set<string>>();
  for (const other of selected) {
    const otherFlavor = serverGpuFlavor(other, plan, gpuFlavors, sourceKind);
    if (!otherFlavor || share(other.source_id) === 'nothing') continue;
    for (const [key, units] of gpuUnits(otherFlavor.gpus)) {
      asked.set(key, (asked.get(key) ?? 0) + units);
      askers.set(key, (askers.get(key) ?? new Set()).add(other.name));
    }
  }
  const reasons: string[] = [];
  for (const short of gpuShortfalls(vgpus, asked, capacity.pools)) {
    if (short.kind === 'refused') reasons.push(`Placement refuses the request for ${short.pool} of ${where} (${short.detail}): no server is created with flavor ${pyRepr(flavor.name)}.`);
    else if (short.kind === 'group')
      reasons.push(`Its flavor asks ${short.pool} of ${where} for ${short.needed} units on one provider, and none has ${short.needed} free (the most on one is ${short.free}).`);
    else {
      const across = short.kind === 'class' ? ' across its pools' : '';
      reasons.push(`The plan's VMs ask ${short.pool} of ${where} for ${counted(short.needed, 'unit')}${across}, more than its ${short.free} free (${firstNames(askers.get(short.key) ?? [])}).`);
    }
  }
  if (reasons.length) out.push(['DST_GPU_CAPACITY_INSUFFICIENT', [...new Set(reasons)].join(' ')]);
  return out;
}

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
      // what its port carries that the migration does not, as the API's Nic (SDD §4.2, §9.3, plan task N2b)
      allowed_address_pairs: n.allowed_address_pairs ?? [],
      floating_ips: n.floating_ips ?? [],
      dns_name: n.dns_name ?? null,
      dns_domain: n.dns_domain ?? null,
    })),
    power_state: spec.power_state ?? 'running',
    os_type: spec.os_type ?? 'rhel9',
    host: spec.host ?? null,
    tags: spec.tags ?? {},
    flavor_extra_specs: spec.flavor_extra_specs ?? {},
    gpus: spec.gpus ?? [],
    cbt_enabled: spec.cbt_enabled === undefined ? null : spec.cbt_enabled,
    snapshot_count: spec.snapshot_count ?? 0,
    tools_ok: spec.tools_ok === undefined ? null : spec.tools_ok,
    change_rate_bps: spec.change_rate_mibps === undefined ? null : spec.change_rate_mibps * MiB,
    disk_bytes: diskBytes,
    used_bytes: usedBytes,
    guest_os: identifyGuestOs(spec.os_type ?? 'rhel9'),
  };
}

/** What the demo's `g1.vgpu` flavor asks for: one vGPU with the trait of the A100-4C type (SDD §22.10). */
const GPU_ML_SPECS: Record<string, string> = { 'resources:VGPU': '1', 'trait:CUSTOM_NVIDIA_A100_4C': 'required' };

/** The domain the demo source publishes its web servers' names in, which its destinations hold no zone of (SDD §16, plan task N2b). */
const DEMO_DNS_DOMAIN = 'example.com.';
/** The keepalived address the demo's database pair shares (allowed_address_pairs, SDD §16). */
const DEMO_DB_VIP = '10.20.1.250';

const OS_VMS: VmSpec[] = [
  { id: 'os-0a11', name: 'web-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 12 }], nics: [{ network: 'tenant-web', floating_ips: ['203.0.113.80'], dns_name: 'web-01', dns_domain: DEMO_DNS_DOMAIN }], tags: { app: 'shop', tier: 'web' }, host: 'compute-01' },
  { id: 'os-0a12', name: 'web-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'ubuntu 22.04', disks: [{ size_gb: 40, used_gb: 14 }], nics: [{ network: 'tenant-web', dns_name: 'web-02', dns_domain: DEMO_DNS_DOMAIN }], tags: { app: 'shop', tier: 'web' }, host: 'compute-02' },
  { id: 'os-0a13', name: 'web-03', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, disks: [{ size_gb: 40, used_gb: 13 }], nics: [{ network: 'tenant-web', dns_name: 'web-03', dns_domain: DEMO_DNS_DOMAIN }], tags: { app: 'shop', tier: 'web' }, host: 'compute-03' },
  { id: 'os-0a14', name: 'web-cache-01', project: 'shop', flavor: 'm1.small', disks: [{ size_gb: 30, used_gb: 9 }], nics: [{ network: 'tenant-web' }], tags: { app: 'shop', tier: 'cache' } },
  { id: 'os-0b21', name: 'api-gw-01', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'rocky 9.4', disks: [{ size_gb: 60, used_gb: 21 }], tags: { app: 'gateway' } },
  { id: 'os-0b22', name: 'api-gw-02', project: 'shop', flavor: 'm1.medium', vcpus: 4, ram_mb: 8192, os_type: 'debian 12', disks: [{ size_gb: 60, used_gb: 20 }], tags: { app: 'gateway' } },
  { id: 'os-0b31', name: 'mq-broker-01', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 46, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0b32', name: 'mq-broker-02', project: 'platform', flavor: 'm1.large', vcpus: 8, ram_mb: 16384, disks: [{ size_gb: 40, used_gb: 15 }, { size_gb: 100, used_gb: 51, volume_type: 'tripleo-ceph-ssd' }], tags: { app: 'rabbitmq' } },
  { id: 'os-0c41', name: 'ad-dc-01', project: 'platform', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', disks: [{ size_gb: 80, used_gb: 38 }], nics: [{ network: 'tenant-infra', mtu: 1500 }], tags: { app: 'active-directory' } },
  { id: 'os-0d51', name: 'db-pg-01', project: 'finance', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 18 }, { size_gb: 600, used_gb: 410, volume_type: 'tripleo-ceph-ssd', encrypted: true }], nics: [{ network: 'tenant-db', allowed_address_pairs: [DEMO_DB_VIP] }], tags: { app: 'ledger', tier: 'database' }, change_rate_mibps: 8 },
  { id: 'os-0d52', name: 'db-mysql-02', project: 'shop', flavor: 'db.xlarge', vcpus: 16, ram_mb: 65536, disks: [{ size_gb: 50, used_gb: 16 }, { size_gb: 520, used_gb: 300, volume_type: 'tripleo-ceph-ssd' }], nics: [{ network: 'tenant-db', allowed_address_pairs: [DEMO_DB_VIP] }], tags: { app: 'shop', tier: 'database' }, change_rate_mibps: 5 },
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
  // the demo's GPU VM (SDD §22.10): its flavor asks for one vGPU of the type its trait names, as its source reports it (§22.2)
  { id: 'os-1a03', name: 'gpu-ml-01', project: 'media', flavor: 'g1.vgpu', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 100, used_gb: 58 }], flavor_extra_specs: GPU_ML_SPECS, gpus: gpuRequests(GPU_ML_SPECS), tags: { app: 'ml-training' }, change_rate_mibps: 2 },
];

const VMW_VMS: VmSpec[] = [
  { id: 'vm-5001', name: 'vm-erp-app-01', project: 'erp', vcpus: 8, ram_mb: 32768, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 120, used_gb: 64, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp' }, change_rate_mibps: 4 },
  { id: 'vm-5002', name: 'vm-erp-db-01', project: 'erp', vcpus: 16, ram_mb: 131072, os_type: 'rhel8', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 100, used_gb: 30, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 800, used_gb: 590, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'erp', tier: 'database' }, change_rate_mibps: 10 },
  { id: 'vm-5003', name: 'vm-fileserver-01', project: 'erp', vcpus: 4, ram_mb: 16384, os_type: 'windows2019', cbt_enabled: false, tools_ok: true, disks: [{ size_gb: 100, used_gb: 40, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 1500, used_gb: 1100, kind: 'vmdk', volume_type: null, device: 'scsi0:1' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'files' } },
  { id: 'vm-5004', name: 'vm-print-01', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2016', cbt_enabled: true, tools_ok: false, disks: [{ size_gb: 60, used_gb: 22, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }, { size_gb: 20, used_gb: 3, kind: 'vmdk', volume_type: null, device: 'scsi0:1', independent: true }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'print' } },
  { id: 'vm-5005', name: 'vm-hr-portal', project: 'erp', vcpus: 4, ram_mb: 8192, os_type: 'Ubuntu 24.04.1 LTS', cbt_enabled: true, tools_ok: true, snapshot_count: 2, disks: [{ size_gb: 80, used_gb: 31, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'hr' } },
  { id: 'vm-5006', name: 'vm-legacy-win2008', project: 'erp', vcpus: 2, ram_mb: 4096, os_type: 'windows2008', cbt_enabled: false, tools_ok: false, disks: [{ size_gb: 60, used_gb: 41, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], tags: { app: 'legacy-crm' } },
  // the demo's GPU VMs (SDD §22.10): a vGPU, a MIG-backed vGPU, and a DirectPath I/O GPU on a VM powered off, which only a
  // cold migration takes (§22.6)
  { id: 'vm-5007', name: 'dc2-ml-01', project: 'erp', vcpus: 8, ram_mb: 32768, os_type: 'rhel9', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 120, used_gb: 70, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], gpus: [gpuDevice({ kind: 'vgpu', profile: 'grid_a100-4c' })], tags: { app: 'ml-training' }, change_rate_mibps: 2 },
  { id: 'vm-5008', name: 'dc2-ml-02', project: 'erp', vcpus: 16, ram_mb: 65536, os_type: 'rhel9', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 200, used_gb: 120, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], gpus: [gpuDevice({ kind: 'vgpu', profile: 'grid_a100-3-20c' })], tags: { app: 'ml-inference' }, change_rate_mibps: 3 },
  { id: 'vm-5009', name: 'dc2-cuda-01', project: 'erp', vcpus: 16, ram_mb: 131072, os_type: 'rhel9', power_state: 'stopped', cbt_enabled: true, tools_ok: true, disks: [{ size_gb: 250, used_gb: 160, kind: 'vmdk', volume_type: null, device: 'scsi0:0' }], nics: [{ network: 'VM Network ERP', mtu: 1500 }], gpus: [gpuDevice({ kind: 'passthrough', vendor_id: '10de', device_id: '20b5' })], tags: { app: 'cuda-batch' } },
];

const LAB_VMS: VmSpec[] = [
  { id: 'lab-2b01', name: 'lab-k8s-master', project: 'lab', flavor: 'm1.large', vcpus: 4, ram_mb: 16384, disks: [{ size_gb: 80, used_gb: 25, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
  { id: 'lab-2b02', name: 'lab-k8s-worker-1', project: 'lab', flavor: 'm1.large', vcpus: 8, ram_mb: 32768, disks: [{ size_gb: 120, used_gb: 48, volume_type: 'lvm' }], tags: { app: 'k8s-lab' } },
];

// -------------------------------------------------------------------------------------------
// Providers
// -------------------------------------------------------------------------------------------

function conversionHost(name: string, address: string, sshHostKeys: string[] = []) {
  return { manage: true, name, flavor: 'm1.large', external_network: 'provider-ext', image: 'rhel-9.4-conversion', ssh_user: 'cloud-user', address, ssh_allowed_cidr: null, ssh_key_secret: null, ssh_host_keys: sshHostKeys };
}

/** The demo destinations' conversion host keys as their operator declares them: the API demo's (SDD §10, §26.3; plan task ST7),
 * synthetic - made up from each host's name, so no private half exists anywhere. */
const DEMO_HOST_KEYS = {
  dst: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPG3KNdzNY5BMzcxmBiodxJzFyuk3o+NUwZvY9B8BPyn os-migrate-conv-dst',
  dc3: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONXdEMQsyylyTGj3eyOnSgAxp5zE4Y8rF+Fpabez7MG os-migrate-conv-dc3',
};

export function buildProviders(now: number): Provider[] {
  // what each seeded destination's check read of its cloud: its GPU capacity among it (SDD §10, §22.3)
  const inventories = buildInventories(now);
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
      // the demo cluster's Ceph access, its key as written (SDD §10, §28.4, plan task CE11)
      ceph: { fsid: DEMO_CEPH_FSID, monitors: ['ceph-mon-0.dc1.example.com', 'ceph-mon-1.dc1.example.com'], client: 'seamless', key_updated_at: iso(now - 30 * 24 * 60 * 60_000) },
      capabilities: {
        ...checkOf({ id: 'rhosp17-dc1', kind: 'openstack', role: 'source', release: null, policy_profile: null }, iso(now - 4 * 60_000)),
        // its project credentials' check, by project id (SDD §10, §27.7, plan task TC8)
        projects: projectsReport('rhosp17-dc1', SEEDED_PROJECT_CREDENTIALS['rhosp17-dc1']!, iso(now - 4 * 60_000)),
      },
      // an application credential per project, written by an admin two days ago (SDD §4.2, §27.4, plan task TC8)
      project_credentials: Object.fromEntries(
        SEEDED_PROJECT_CREDENTIALS['rhosp17-dc1']!.map((id) => [id, { secret: projectSecretName('rhosp17-dc1', id), updated_at: iso(now - 2 * 24 * 3600_000) }]),
      ),
      status: 'ok',
      // an ok check leaves no message, as the API's (SDD §10, review RV23-04)
      status_message: null,
      last_checked_at: iso(now - 4 * 60_000),
      distribution: 'rhosp',
      release: null,
      policy_profile: null,
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
      capabilities: vmwareCheck(VMW_VMS.map(makeVm)),
      status: 'degraded',
      status_message: 'Two ESXi hosts in maintenance mode; CBT queries are slow.',
      last_checked_at: iso(now - 11 * 60_000),
      distribution: 'vmware',
      release: null,
      policy_profile: null,
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
      release: null,
      policy_profile: null,
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
      release: null,
      policy_profile: null,
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
      conversion_host: conversionHost('seamless-conv-dst', '198.51.100.31', [DEMO_HOST_KEYS.dst]),
      // an admin token (SDD §10): jump-host-01 keeps its address on the shared provider-ext (§6.6)
      capabilities: checkOf(
        { id: 'rhoso-prod', kind: 'rhoso', role: 'destination', release: null, policy_profile: null, gpu_profiles: DEMO_GPU_PROFILES },
        iso(now - 3 * 60_000),
        inventories['rhoso-prod'],
      ),
      status: 'ok',
      // an ok check leaves no message, as the API's (SDD §10, review RV23-04)
      status_message: null,
      last_checked_at: iso(now - 3 * 60_000),
      distribution: 'rhoso',
      release: null,
      policy_profile: null,
      // the vGPU profiles of its GPU flavors, which no API names (SDD §22.3, §22.10)
      gpu_profiles: { ...DEMO_GPU_PROFILES },
      // its OVN BGP agent announces tenant networks, which no API shows (SDD §16, §23.6, plan task N2b)
      announces_tenant_networks: true,
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
      release: null,
      policy_profile: null,
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
    {
      // the API demo's community destination (SDD §10, plan T9): a Kolla-Ansible 2026.2 cloud
      id: 'hibiscus-dc3',
      name: 'OpenStack 2026.2 Hibiscus (DC3)',
      kind: 'openstack',
      role: 'destination',
      endpoint: 'https://keystone.dc3.example.com:5000/v3',
      cloud: 'hibiscus',
      credentials_secret: null,
      region: 'RegionOne',
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: conversionHost('os-migrate-conv-dc3', '192.0.2.60', [DEMO_HOST_KEYS.dc3]),
      capabilities: checkOf(
        { id: 'hibiscus-dc3', kind: 'openstack', role: 'destination', release: '2026.2', policy_profile: null, gpu_profiles: DEMO_GPU_PROFILES },
        iso(now - 6 * 60_000),
        inventories['hibiscus-dc3'],
      ),
      status: 'ok',
      // an ok check leaves no message, as the API's (SDD §10, review RV23-04)
      status_message: null,
      last_checked_at: iso(now - 6 * 60_000),
      distribution: 'kolla',
      release: '2026.2',
      policy_profile: null,
      gpu_profiles: { ...DEMO_GPU_PROFILES },
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    },
  ];
}

/**
 * A checked destination's inventory (SDD §10): the community destination shares it, as the API demo's does, each with
 * its own copy - the demo's GPU flavors among its flavors, and what its Placement holds for their pools (§22.10). Its GPU
 * flavors are not kept: each read finds them with the profiles its provider declares then (`readDestinationGpus`).
 */
function destinationInventory(readAt: string): DestinationInventory {
  const flavors: DestinationFlavor[] = [
    { name: 'm1.small', vcpus: 2, ram_mb: 4096, disk_gb: 40, extra_specs: {} },
    { name: 'm1.medium', vcpus: 4, ram_mb: 8192, disk_gb: 80, extra_specs: {} },
    { name: 'm1.large', vcpus: 8, ram_mb: 32768, disk_gb: 160, extra_specs: {} },
    { name: 'db.xlarge', vcpus: 16, ram_mb: 131072, disk_gb: 200, extra_specs: { 'hw:cpu_policy': 'dedicated' } },
    ...DEMO_GPU_FLAVORS.map((f) => ({ ...f, extra_specs: { ...f.extra_specs } })),
  ];
  const destination: DestinationInventory = {
    networks: { 'tenant-web': 1442, 'tenant-app': 1442, 'tenant-db': 1442, 'tenant-infra': 1442, analytics: 1442, 'erp-net': 1442, 'provider-ext': 1500 },
    flavors,
    volume_types: ['ceph-ssd', 'ceph-hdd', 'ceph-nvme', 'netapp-nfs'],
    quotas: {
      shop: { cores: 120, ram_mb: 262144, instances: 40, volumes: 80, gigabytes: 8000 },
      finance: { cores: 96, ram_mb: 393216, instances: 20, volumes: 40, gigabytes: 6000 },
      platform: { cores: 64, ram_mb: 131072, instances: 30, volumes: 60, gigabytes: 4000 },
      analytics: { cores: 128, ram_mb: 524288, instances: 16, volumes: 32, gigabytes: 12000 },
      erp: { cores: 64, ram_mb: 262144, instances: 20, volumes: 40, gigabytes: 9000 },
    },
    projects: ['shop', 'finance', 'platform', 'analytics', 'erp', 'media'],
    // the token's roles, read live with the inventory, as the API demo's (SDD §10, 7f4cc5b5, review RV20-10)
    roles: [...DEMO_TOKEN_ROLES],
    // where a NIC's addresses must lie (SDD §10): the mock VMs' addresses are in 10.20.0.0/16
    subnets: [
      ...['tenant-web', 'tenant-app', 'tenant-db', 'tenant-infra', 'analytics', 'erp-net'].map((network) => ({
        id: `sub-${network}-v4`,
        name: `${network}-v4`,
        network,
        network_id: `net-${network}`,
        cidr: '10.20.0.0/16',
        ipv6_address_mode: null,
        ipv6_ra_mode: null,
      })),
      { id: 'sub-provider-ext-v4', name: 'provider-ext-v4', network: 'provider-ext', network_id: 'net-provider-ext', cidr: '203.0.113.0/24', ipv6_address_mode: null, ipv6_ra_mode: null },
    ],
    // what the destination's Placement holds for its GPU flavors' pools, which each read takes its pools from (SDD §22.3)
    gpu_capacity: { status: 'ok', reason: null, detail: null, read_at: readAt, pools: demoGpuPools() },
    // no floating IP held detached, and Neutron's DNS integration without the demo source's zone (SDD §10, §16, plan task N2b)
    floating_ips: [],
    dns: { extension: true, zones: ['apps.ocp.example.com.'] },
  };
  // what a NIC's network name resolves to (SDD §10): the credential's own networks, and a provider network another project shares
  destination.network_details = Object.entries(destination.networks).map(([name, mtu]) => ({
    id: `net-${name}`,
    name,
    mtu,
    project_id: name === 'provider-ext' ? 'admin' : 'migration',
    own: name !== 'provider-ext',
    shared: name === 'provider-ext',
  }));
  return destination;
}

/**
 * A destination's inventory that lists nothing (SDD §10): its token's roles, as the API demo's, and no GPU flavor, so a
 * read finds its GPU capacity ok with no pools and sends Placement nothing (§22.3).
 */
export function emptyDestinationInventory(): DestinationInventory {
  return { networks: {}, flavors: [], volume_types: [], quotas: {}, projects: [], roles: [...DEMO_TOKEN_ROLES] };
}

/** The seeded providers' inventories at `now`: what a read finds there, a destination's GPUs by `readDestinationGpus` (SDD §10). */
export function buildInventories(now: number = Date.now()): Record<string, VMRef[] | DestinationInventory> {
  const readAt = iso(now);
  return {
    'rhosp17-dc1': OS_VMS.map(makeVm),
    'vcenter-hq': VMW_VMS.map(makeVm),
    'community-lab': LAB_VMS.map(makeVm),
    'rhoso-prod': destinationInventory(readAt),
    'rhoso-staging': emptyDestinationInventory(),
    'hibiscus-dc3': destinationInventory(readAt),
  };
}

// -------------------------------------------------------------------------------------------
// Estimator (SDD §9.1) and eligibility (§9.2)
// -------------------------------------------------------------------------------------------

/** A strategy's estimate as the API's validation computes it (§9.1, §16), each part shown to 0.1 s. */
export function estimate(vm: VMRef, strategy: Strategy, plan: Plan, reasons: string[], tracked?: readonly string[] | null): Estimate {
  const e = estimateStrategy(vm, strategy, paramsFor(plan), plan.downtime_slo_s, tracked);
  const round = (n: number) => Math.round(n * 10) / 10;
  // the API's keys (SDD §4, ADV-16): the rounded parts add up to the downtime
  const downtime_parts = Object.fromEntries(DOWNTIME_PARTS.map((part) => [part, round(e.downtime_parts[part] ?? 0)]));
  const downtime = round(DOWNTIME_PARTS.reduce((sum, part) => sum + (downtime_parts[part] ?? 0), 0));
  return {
    ...e,
    eligible: reasons.length === 0,
    reasons,
    precopy_s: round(e.precopy_s),
    downtime_s: downtime,
    total_s: round(e.precopy_s + downtime),
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
    // its own key only, as eligibility and HANDOVER_BACKEND_UNMAPPED read it: a type named "constructor" is no backend (review RV21-11)
    const type = disk.volume_type ?? '';
    const target = Object.hasOwn(plan.handover.backend_map, type) ? plan.handover.backend_map[type] : undefined;
    if (family !== 'other' && (!target || dst.length === 0)) continue;
    const { error } = resolveDestination(family, splitHost(disk.pool)[1], target ?? '', dst, providerLabel(destination));
    if (error) reasons.push(`${disk.name || disk.id}: ${error}`);
  }
  return reasons;
}


/** The VM with each NIC's addresses as the plan gives them (SDD §9.2): its override's, else its own. */
function withPlannedAddresses(plan: Plan, vm: VMRef): VMRef {
  return { ...vm, nics: vm.nics.map((nic) => ({ ...nic, fixed_ips: nicTarget(plan, vm.source_id, nic).addresses })) };
}

/** The source's compute floor of a storage handover: its delete_on_termination update of the source server (the API's
 * planning/selector.HANDOVER_COMPUTE_FLOOR, SDD §9.2, §21.4). */
const HANDOVER_COMPUTE_FLOOR = '2.85';

/** Disks by name, else id, in their order: the API's `_ids`. */
const diskIds = (disks: VMRef['disks']) => disks.map((d) => d.name || d.id).join(', ');

/**
 * Every strategy of the source's family with its ineligibility reasons, word for word as the API's
 * planning/selector.eligibility (SDD §9.2, §16): the shared eligibility_cases.json pins both (review RV20-08).
 */
/** Why a migration that holds an earlier attempt's destination volumes takes no storage handover: the API's words (SDD §9.2). */
export const HELD_VOLUMES_REASON =
  'an earlier attempt keeps its destination volumes for a retry, which a storage handover would leave behind: migrate it warm again, which uses them, or cold, whose cutover removes them first';

export function eligibility(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  source: Provider,
  destination: Provider,
  findings: Finding[],
  holdsVolumes = false,
): Partial<Record<Strategy, string[]>> {
  const result: Partial<Record<Strategy, string[]>> = {};
  const multiattach = vm.disks.filter((d) => d.multiattach);
  const inError = vm.power_state === 'error' || vm.power_state === 'transitioning';
  if (sourceKind === 'vmware') {
    result.vmware_cold = inError ? [`source VM is in ${vm.power_state} state`] : [];
    const warm: string[] = [];
    if (vm.cbt_enabled !== true) warm.push('Changed Block Tracking (CBT) is not enabled');
    const independent = vm.disks.filter((d) => d.independent);
    if (independent.length) warm.push(`independent disk(s): ${diskIds(independent)}`);
    // vSphere takes no snapshot of a VM with a DirectPath I/O device, and the kit reads every copy from one; powered off,
    // its snapshot holds no device state - the API's rule (SDD §9.2, §22.6, plan task G5)
    const passthrough = vmGpus(vm).filter((g) => g.kind === 'passthrough');
    if (passthrough.length) {
      const devices = gpuNames(passthrough);
      warm.push(`PCI passthrough device(s) ${devices}: vSphere takes no snapshot of a VM with one - power it off, validate it again and migrate it cold`);
      if (vm.power_state !== 'stopped')
        result.vmware_cold.push(
          `PCI passthrough device(s) ${devices} on a VM that is not powered off: power it off, validate it again and migrate it cold`,
        );
    }
    result.vmware_warm = warm;
  } else {
    const copy: string[] = [];
    if (inError) copy.push(`source VM is in ${vm.power_state} state`);
    if (!source.conversion_host) copy.push('no conversion host configured on the source provider');
    if (!destination.conversion_host) copy.push(`no conversion host configured on ${providerLabel(destination)}`);
    // through Nova os-migrate recreates each port with exactly one fixed IP; through Neutron every one (SDD §9.2)
    const planned = withPlannedAddresses(plan, vm);
    const oddPorts = portsWithoutOneFixedIp(planned);
    if (oddPorts.length && plan.port_creation_mode === 'nova') copy.push(`port(s) without exactly one fixed IP, which Nova cannot recreate: ${describePorts(oddPorts)}`);
    result.cold = [...copy];
    result.warm = [...copy, ...(multiattach.length ? [`multi-attach disk(s): ${diskIds(multiattach)}`] : [])];
    const handover: string[] = [];
    if (!plan.handover.enabled) handover.push('storage handover is not enabled for this plan');
    const notVolumes = vm.disks.filter((d) => d.kind !== 'volume');
    if (notVolumes.length) handover.push(`disk(s) not backed by Cinder volumes: ${diskIds(notVolumes)}`);
    const unmapped = vm.disks.filter((d) => d.kind === 'volume' && !Object.hasOwn(plan.handover.backend_map, d.volume_type || ''));
    if (unmapped.length) handover.push(`volume type(s) without a backend of ${providerLabel(destination)} in handover.backend_map: ${pyNames(unmapped.map((d) => d.volume_type || '<default>'))}`);
    if (multiattach.length) handover.push(`multi-attach disk(s): ${diskIds(multiattach)}`);
    // through Nova the destination keeps one address per port; through Neutron every one (SDD §7.3 step 8)
    const several = planned.nics.filter((n) => n.fixed_ips.length > 1);
    if (several.length && plan.port_creation_mode === 'nova') handover.push(`port(s) with several fixed IPs, of which one would be kept: ${describePorts(several)}`);
    const encrypted = vm.disks.filter((d) => d.encrypted);
    if (encrypted.length) handover.push(`encrypted disk(s), which Cinder cannot unmanage: ${diskIds(encrypted)}`);
    handover.push(...storageReasons(vm, plan, source, destination));
    if (!source.capabilities.admin) handover.push('admin rights are required on the source cloud');
    if (!destination.capabilities.admin) handover.push(`admin rights are required on ${providerLabel(destination)}`);
    // a gap this strategy alone has is its reason, never a finding: the update goes to the source server (SDD §9.2, §21.4)
    const compute = parseMicroversion(source.capabilities.compute_microversion);
    const floor = parseMicroversion(HANDOVER_COMPUTE_FLOOR)!;
    if (compute && (compute[0] - floor[0] || compute[1] - floor[1]) < 0) {
      handover.push(
        `the source's compute API ${String(source.capabilities.compute_microversion)} is below ${HANDOVER_COMPUTE_FLOOR} (the handover's delete_on_termination update of the source server)`,
      );
    }
    // a handover never removes what an earlier attempt left at the destination (SDD §9.2, review 39 lead L1)
    if (holdsVolumes) handover.push(HELD_VOLUMES_REASON);
    result.storage_handover = handover;
  }
  for (const finding of findings) {
    if (finding.severity !== 'blocker') continue;
    for (const reasons of Object.values(result)) reasons.push(`blocked by finding ${finding.code}`);
  }
  return result;
}

// -------------------------------------------------------------------------------------------
// Pre-flight findings (subset of SDD §9.3)
// -------------------------------------------------------------------------------------------

/** What lets another project's shared network take the port, by the destination's policy profile: the API's _PORT_ROLE_ADVICE (SDD §21.5). */
const PORT_ROLE_ADVICE: Record<PolicyProfile, string> = {
  secure_rbac: "the destination credential has the service role for the migration window (Neutron's secure-RBAC defaults); admin is not needed for this",
  legacy: "the destination credential has the advsvc role (Neutron's legacy rules)",
  unknown:
    "a check of the provider detects its series, or the provider declares its policy profile or series, so the check counts that profile's role: service under secure-RBAC, advsvc under the legacy rules",
};

/** NET_PORT_NOT_PERMITTED's remediation for the destination's policy profile: the API's _port_remediation (review RV18-10). */
function portRemediation(profile: string | undefined): string {
  const counted: PolicyProfile = profile === undefined ? 'legacy' : profile in PORT_ROLE_ADVICE ? (profile as PolicyProfile) : 'unknown';
  return (
    "Send the NIC to a network of the credential's project: map its network to one, or set a NIC override on the VM's migration page. " +
    `Another project's shared network takes it once ${PORT_ROLE_ADVICE[counted]}. ` +
    'An external network that is not shared needs the admin role: Nova attaches it for no other.'
  );
}

/** Like the API (SDD §9.3): every entry of the VM checked before anything stops, with the API's messages. */
function nicOverrideFindings(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  destinationNetworks: Record<string, number | null>,
  context: { subnets?: DestinationSubnet[]; selected?: VMRef[]; networkDetails?: DestinationNetwork[]; destination?: string },
): Array<['MAP_NIC_OVERRIDE' | 'MAP_NIC_OVERRIDE_INVALID', string]> {
  const where = context.destination ?? 'the destination';
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
    if (!Object.hasOwn(destinationNetworks, network)) {
      problems.push(
        `${mac}: ${where} has no network ${pyRepr(network)}` +
          (entries[mac]?.network ? '' : ` yet: pre-staging would create it, but its subnets cannot be checked before; name an existing network of ${where}`),
      );
      continue;
    }
    // the network the cutover resolves the name to; MAP_NETWORK_AMBIGUOUS names one it cannot
    const resolved = resolveNetwork(networkList({ networks: destinationNetworks, network_details: context.networkDetails }), network).network;
    if (!resolved) continue;
    const subnets = subnetsOf(context.subnets ?? [], resolved);
    const cidrs = subnets.map((s) => s.cidr);
    const outside = addresses.filter((a) => !cidrs.some((c) => ipInCidr(a, c)));
    if (outside.length) problems.push(`${mac}: ${outside.join(', ')} ${outside.length === 1 ? 'lies' : 'lie'} in no subnet of ${pyRepr(network)}`);
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

/** Where a server is in the source's Heat stacks, as the API's StackPlace (SDD §10, plan task N2). */
export interface StackPlace {
  stack: string;
  resource: string;
  group: string | null;
  group_type: string | null;
}

/** The source's Heat stacks by the servers they hold, as the API's StackRead (SDD §10, plan task N2). */
export interface StackRead {
  status: 'ok' | 'unknown';
  reason: 'forbidden' | 'error' | null;
  detail: string | null;
  servers: Record<string, StackPlace>;
}

/** A member of an Octavia pool of the source, as the API's PoolMember (SDD §10, plan tasks N2, N9). */
export interface PoolMember {
  load_balancer: string;
  pool: string;
  /** The pool's id: what a plan's network_cutover.lb_pools maps (SDD §8.5). */
  pool_id: string;
  address: string;
  port: number;
  network: string | null;
}

/** The members of the source's Octavia pools, as the API's MemberRead (SDD §10, plan task N2). */
export interface MemberRead {
  status: 'ok' | 'unknown';
  reason: 'forbidden' | 'error' | null;
  detail: string | null;
  members: PoolMember[];
}

/**
 * The demo source's Heat and Octavia reads (SDD §16, plan task N2): on RHOSP 17.1 — DC1 one stack, example-mq, whose
 * autoscaling group holds mq-broker-01 and mq-broker-02, and one pool of a load balancer, example-mq-pool of
 * example-mq-lb, whose members are their addresses on AMQP's port; every other source reads both ok and empty.
 */
export function sourceReads(providerId: string, vms: readonly VMRef[]): { stacks: StackRead; lbMembers: MemberRead } {
  const stacks: StackRead = { status: 'ok', reason: null, detail: null, servers: {} };
  const lbMembers: MemberRead = { status: 'ok', reason: null, detail: null, members: [] };
  if (providerId !== 'rhosp17-dc1') return { stacks, lbMembers };
  for (const vm of vms.filter((v) => MQ_BROKERS.includes(v.source_id))) {
    stacks.servers[vm.source_id] = { stack: 'example-mq', resource: 'broker_group', group: 'broker_group', group_type: 'OS::Heat::AutoScalingGroup' };
    for (const nic of vm.nics)
      for (const address of nic.fixed_ips) lbMembers.members.push({ load_balancer: 'example-mq-lb', pool: 'example-mq-pool', pool_id: 'example-mq-pool', address, port: 5672, network: nic.network });
  }
  return { stacks, lbMembers };
}

/** The demo's message brokers, members of the example-mq stack's autoscaling group (SDD §16). */
const MQ_BROKERS = ['os-0b31', 'os-0b32'];

/** SRC_VM_AUTOSCALED's remediation once the plan acknowledges it, as the API's (SDD §9.3, owner decision 9). */
const AUTOSCALED_ACKNOWLEDGED_REMEDIATION =
  "Keep the group's scaling suspended at the source until the VM is finalized, as the runbook for Heat stacks says: a server the group creates meanwhile is a second live copy.";

/** Why a read of the source's Heat or Octavia is unknown, as the API's _unread: by its HTTP status or kind (SDD §10). */
function unread(service: string, read: StackRead | MemberRead): string {
  if (read.reason === 'forbidden') return `${service} refused the source credential (${read.detail || 'HTTP 403'})`;
  return `its read failed (${read.detail || 'no answer'})`;
}

/**
 * SRC_VM_HEAT_STACK, SRC_VM_AUTOSCALED and SRC_VM_HEAT_UNKNOWN, as the API's _stack_findings (SDD §9.3, plan task N2): an
 * autoscaling group's member blocks until the plan acknowledges it, then warns that it does.
 */
function stackFindings(vm: VMRef, plan: Plan, stacks: StackRead, at: string): Array<[string, string, { severity?: Severity; remediation?: string }?]> {
  if (stacks.status === 'unknown')
    return [['SRC_VM_HEAT_UNKNOWN', `Whether ${vm.name} belongs to a Heat stack or an autoscaling group at ${at} is not known: ${unread('Heat', stacks)}.`]];
  const place = stacks.servers[vm.source_id];
  if (!place) return [];
  const out: Array<[string, string, { severity?: Severity; remediation?: string }?]> = [
    [
      'SRC_VM_HEAT_STACK',
      `${vm.name} belongs to Heat stack ${pyRepr(place.stack)} at ${at} (its resource ${pyRepr(place.resource)}): the stack is not moved, and a stack update there may recreate the server.`,
    ],
  ];
  if (place.group !== null) {
    const text = `${vm.name} is a member of autoscaling group ${pyRepr(place.group)} (${place.group_type}) of Heat stack ${pyRepr(place.stack)} at ${at}: the group may replace the server once the cutover stops it, which leaves two live copies.`;
    if ((plan.acknowledged_findings?.[vm.source_id] ?? []).includes('SRC_VM_AUTOSCALED'))
      out.push(['SRC_VM_AUTOSCALED', `${text} The plan acknowledges it (acknowledged_findings).`, { severity: 'warning', remediation: AUTOSCALED_ACKNOWLEDGED_REMEDIATION }]);
    else out.push(['SRC_VM_AUTOSCALED', text]);
  }
  return out;
}

/**
 * The VM's memberships of the source's Octavia pools, as the API's _memberships (SDD §9.3): each of `members` that is an
 * address of a NIC of the VM, on the NIC's network when the read names the member's, NIC by NIC.
 */
function memberships(vm: VMRef, members: readonly PoolMember[]): PoolMember[] {
  return vm.nics.flatMap((nic) => {
    const addresses = new Set(nic.fixed_ips.map(canonicalIp));
    return members.filter((m) => addresses.has(canonicalIp(m.address)) && (m.network === null || m.network === nic.network));
  });
}

/**
 * SRC_VM_LB_MEMBER and SRC_VM_LB_UNKNOWN, as the API's _member_findings (SDD §9.3, plan task N2): each pool member that is
 * an address of the VM's NIC - on the NIC's network when the read names the member's -, NIC by NIC, each named once; a
 * member of a pool the plan maps is the member helper's, left out (SDD §8.5, plan task N9).
 */
function memberFindings(vm: VMRef, plan: Plan, read: MemberRead, at: string): Array<[string, string]> {
  if (read.status === 'unknown')
    return [['SRC_VM_LB_UNKNOWN', `Whether addresses of ${vm.name} are members of Octavia pools at ${at} is not known: ${unread('Octavia', read)}.`]];
  const mapped = plan.network_cutover?.lb_pools ?? {};
  const held = memberships(vm, read.members)
    .filter((m) => !Object.hasOwn(mapped, m.pool_id))
    .map((m) => `${canonicalIp(m.address)} port ${m.port} in pool ${pyRepr(m.pool)} of load balancer ${pyRepr(m.load_balancer)}`);
  if (!held.length) return [];
  return [['SRC_VM_LB_MEMBER', `Addresses of ${vm.name} are members of Octavia pools at ${at}: ${[...new Set(held)].join('; ')}. The memberships are not moved.`]];
}

/** A DNS domain as the API's _dns_domain compares it: lower case, with its trailing dot (SDD §9.3). */
function dnsDomain(domain: string): string {
  return `${domain.toLowerCase().replace(/\.+$/, '')}.`;
}

/**
 * SRC_PORT_ADDRESS_PAIRS, MAP_FIP_ADDRESS_CHANGE and DST_DNS_INTEGRATION_MISSING, as the API's _port_findings (SDD §9.3,
 * plan task N2b): what an OpenStack VM's ports carry at the source that the migration does not.
 */
function portFindings(vm: VMRef, plan: Plan, floatingIps: readonly string[], dns: DnsRead, at: string, where: string): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  const paired = vm.nics
    .filter((nic) => nic.allowed_address_pairs?.length)
    .map((nic) => `${(nic.mac ?? '?').toLowerCase()} on ${pyRepr(nic.network)} (${(nic.allowed_address_pairs ?? []).join(', ')})`);
  if (paired.length)
    out.push([
      'SRC_PORT_ADDRESS_PAIRS',
      `NICs of ${vm.name} have allowed address pairs, which the import does not carry: ${paired.join('; ')}. An address the VM takes over from a peer is not reachable in ${where}.`,
    ]);
  // os-migrate's `auto` attaches a detached floating IP of the source's address, else creates one with a new address (§23.1)
  const detached = new Set(floatingIps.map(canonicalIp));
  // under keep_address the move keeps each address (SDD §8.3): no new one to warn of
  const moved =
    plan.network_cutover?.floating_ips === 'keep_address'
      ? []
      : [...new Set(vm.nics.flatMap((nic) => (nic.floating_ips ?? []).map(canonicalIp)))].filter((a) => !detached.has(a));
  if (moved.length)
    out.push([
      'MAP_FIP_ADDRESS_CHANGE',
      `Floating IPs of ${vm.name} that ${where} does not hold detached in its project: ${moved.join(', ')}. os-migrate creates a floating IP with a new address for each, so clients, allowlists and DNS records of the old address lose the VM.`,
    ]);
  // Neutron's DNS integration is checked, never driven; what could not be read is not judged (§9.3, §23.5)
  const zones = dns.zones === null ? null : new Set(dns.zones.map(dnsDomain));
  const published = vm.nics.filter((nic) => nic.dns_name && nic.dns_domain);
  const noZone = published.filter((nic) => zones !== null && !zones.has(dnsDomain(nic.dns_domain ?? '')));
  const lacking = dns.extension === false ? published : noZone;
  if (lacking.length) {
    const named = lacking.map((nic) => `${(nic.mac ?? '?').toLowerCase()} as ${nic.dns_name}.${dnsDomain(nic.dns_domain ?? '')}`).join(', ');
    const why: string[] = [];
    if (dns.extension === false) why.push("lacks Neutron's dns-integration extension");
    if (noZone.length) why.push(`has no Designate zone ${[...new Set(noZone.map((nic) => dnsDomain(nic.dns_domain ?? '')))].join(', ')}`);
    out.push([
      'DST_DNS_INTEGRATION_MISSING',
      `NICs of ${vm.name} are published in DNS at ${at} - ${named} - and ${where} ${why.join(' and ')}: the migrated ports are not published.`,
    ]);
  }
  return out;
}

/**
 * NET_PREFIX_SPLIT, as the API's _prefix_split (SDD §9.3, §23.6, plan task N2b): a network of the VM whose VMs the plan's
 * waves put into more than one wave while a provider declares that its cloud announces tenant networks; nothing is judged
 * before the waves are planned.
 */
function prefixSplit(vm: VMRef, plan: Plan, selected: readonly VMRef[], announcers: readonly string[]): Array<[string, string]> {
  const waves = plan.waves ?? [];
  if (!announcers.length || !waves.length) return [];
  const split: string[] = [];
  for (const network of new Set(vm.nics.map((nic) => nic.network))) {
    const holding = new Map<string, Wave>();
    for (const other of selected) {
      const wave = other.nics.some((nic) => nic.network === network) ? waves.find((w) => w.vm_ids.includes(other.source_id)) : undefined;
      if (wave && !holding.has(wave.id)) holding.set(wave.id, wave);
    }
    if (holding.size > 1)
      split.push(`${pyRepr(network)} in waves ${[...holding.values()].sort((a, b) => a.order - b.order).map((w) => pyRepr(w.name)).join(', ')}`);
  }
  if (!split.length) return [];
  return [
    [
      'NET_PREFIX_SPLIT',
      `The plan's waves split networks of ${vm.name} that ${announcers.join(' and ')} ${announcers.length === 1 ? 'announces' : 'announce'} as tenant networks: ${split.join('; ')}. While one wave is cut over and the next is not, each prefix has an origin in both clouds, so its routes cannot be correct.`,
    ],
  ];
}

/**
 * NET_LB_POOL_SPLIT, as the API's _pool_split (SDD §9.3, §23.7): each Octavia pool at the source the VM is a member of - one
 * the plan maps too - whose members among the plan's VMs the plan's waves put into more than one wave; a member that is no
 * VM of the plan, or a VM of the plan in no wave, is not counted, and nothing is judged before the waves are planned.
 */
function poolSplit(vm: VMRef, plan: Plan, read: MemberRead, selected: readonly VMRef[], at: string): Array<[string, string]> {
  const pools = new Map<string, PoolMember>();
  for (const m of memberships(vm, read.members)) if (!pools.has(m.pool_id)) pools.set(m.pool_id, m);
  const waves = plan.waves ?? [];
  if (!pools.size || !waves.length) return [];
  // the members of the VM's pools alone: each VM of the plan is matched against those
  const members = read.members.filter((m) => pools.has(m.pool_id));
  const holding = new Map<string, Map<string, Wave>>([...pools.keys()].map((id) => [id, new Map<string, Wave>()]));
  for (const other of selected) {
    const wave = waves.find((w) => w.vm_ids.includes(other.source_id));
    if (!wave) continue;
    for (const m of memberships(other, members)) holding.get(m.pool_id)!.set(wave.id, wave);
  }
  const split = [...pools.entries()]
    .filter(([id]) => holding.get(id)!.size > 1)
    .map(
      ([id, m]) =>
        `pool ${pyRepr(m.pool)} of load balancer ${pyRepr(m.load_balancer)} in waves ${[...holding.get(id)!.values()]
          .sort((a, b) => a.order - b.order)
          .map((w) => pyRepr(w.name))
          .join(', ')}`,
    );
  if (!split.length) return [];
  return [
    [
      'NET_LB_POOL_SPLIT',
      `The plan's waves split Octavia pools at ${at} that ${vm.name} is a member of: ${split.join('; ')}. While one wave is cut over and the next is not, each pool's members are in both clouds, and its load balancer reaches only those not yet cut over.`,
    ],
  ];
}

/**
 * What the network findings read beside the VM (SDD §9.3, §10, plan task N2b): the destination's detached floating IPs and its
 * DNS as its inventory gives them - none and not read when it gives none -, and the providers that declare that their cloud
 * announces tenant networks, by their labels.
 */
export function networkContext(
  inventory: DestinationInventory | VMRef[] | undefined,
  source: Provider,
  destination: Provider,
): { floatingIps: string[]; dns: DnsRead; announcers: string[] } {
  const dst = inventory && !Array.isArray(inventory) ? inventory : undefined;
  return {
    floatingIps: dst?.floating_ips ?? [],
    dns: dst?.dns ?? { extension: null, zones: null },
    announcers: [source, destination].filter((p) => p.announces_tenant_networks).map(providerLabel),
  };
}

/** SRC_VM_DUPLICATE_NAME's remediation for two VMware VMs the migration kit names alike, as the API's (SDD §9.3). */
const KIT_TWIN_REMEDIATION =
  'Rename one of the VMs in vCenter: the migration kit names a VM\'s server, ports and volumes after its name as it writes it, so the two would share them - in separate plans too, when they go to one destination project.';

export function preflight(
  vm: VMRef,
  sourceKind: ProviderKind,
  plan: Plan,
  destinationNetworks: Record<string, number | null>,
  /** The destination's subnets, its networks with their owners, its token's roles, and the plan's selected VMs - `vm` alone
   * when not given, as the API's all_vms (SDD §9.3). */
  context: {
    subnets?: DestinationSubnet[];
    selected?: VMRef[];
    networkDetails?: DestinationNetwork[];
    roles?: string[];
    profile?: string;
    destination?: string;
    /**
     * The destination's flavors by name, and those of them that ask for a GPU, as its inventory lists them with the
     * profiles it declares: what a GPU VM's mapping names (SDD §22.3, §22.4).
     */
    flavors?: string[];
    gpuFlavors?: GpuFlavor[];
    /** The destination's GPU capacity as the validation reads it (SDD §22.3, §22.5); `ok` with no pools when not given. */
    gpuCapacity?: GpuCapacity;
    /** What a cutover of each selected VM will still create, by source id - all of it when not given (SDD §9.3, §22.5). */
    shares?: Readonly<Record<string, QuotaShare>>;
    /** The source provider's declared vGPU profiles by flavor, which make a CUSTOM_ class a vGPU (SDD §22.2, §22.3). */
    gpuProfiles?: Record<string, string>;
    /** The source provider's label, which a message about the source names (SDD §1 naming, §9.3). */
    source?: string;
    /** The source's Heat stacks and Octavia pool members as its inventory reads them; ok and empty when not given (SDD §10). */
    stacks?: StackRead;
    lbMembers?: MemberRead;
    /** The destination's detached floating IPs and its DNS, none and not read when not given, and the labels of the providers
     * that declare that their cloud announces tenant networks (SDD §9.3, §10, plan task N2b). */
    floatingIps?: string[];
    dns?: DnsRead;
    announcers?: string[];
  } = {},
): Finding[] {
  const findings: Finding[] = [];
  // the destination provider a message about the destination cloud names, as the API's (SDD §1 naming, §9.3)
  const where = context.destination ?? 'the destination';
  const add = (code: string, message: string, own: { severity?: Severity; remediation?: string } = {}) => {
    // the catalog's severity, strategies and remediation, unless the finding gives its own: the API's finding() (SDD §9.3)
    const entry = FINDING_CATALOG[code]!;
    findings.push({ code, severity: own.severity || entry.severity, message, remediation: own.remediation || entry.remediation, strategies: [...entry.strategies] });
  };

  // every message below as the API words it, over the shared finding cases (SDD §9.3, §16)
  if (vm.power_state === 'error') add('SRC_VM_ERROR_STATE', `Source VM ${vm.name} is in error state.`);
  // a task Nova runs whatever the status is named, and meets its condition beside an error (SDD §9.3, review PV-05)
  if (vm.power_state === 'transitioning' || vm.task_state)
    add(
      'SRC_VM_TRANSITIONAL_STATE',
      `Source VM ${vm.name} has a Nova task in flight (${vm.task_state || 'resize, migration, rescue, rebuild or reboot'}); a stop or snapshot would fail.`,
    );
  // the migration kit names a VMware VM's server, ports and volumes after its name as it writes it, and finds a volume
  // again by that name: from a VMware source the kit's names decide, the same name included (SDD §7.2, §9.3, reviews
  // RV26-04, RV28-04)
  const kit = sourceKind === 'vmware' ? kitVmName(vm.name) : null;
  // the other selected VMs of that name, by their project and id, as the API words them (SDD §9.3)
  const twins = kit === null ? (context.selected ?? [vm]).filter((v) => v.source_id !== vm.source_id && v.name === vm.name) : [];
  const kin = kit ? (context.selected ?? [vm]).filter((v) => v.source_id !== vm.source_id && kitVmName(v.name) === kit) : [];
  if (twins.length)
    add('SRC_VM_DUPLICATE_NAME', `Another selected VM is also named ${pyRepr(vm.name)} (${pyNames(twins.map((t) => `${t.project || '-'}/${t.source_id}`))}); os-migrate selects workloads by name.`);
  else if (kin.length)
    add(
      'SRC_VM_DUPLICATE_NAME',
      `Another selected VM has the name ${pyRepr(kit!)} as the migration kit writes it (${pyNames(kin.map((t) => `${t.project || '-'}/${t.source_id}`))}); the kit names the VM's server, ports and volumes after it.`,
      { remediation: KIT_TWIN_REMEDIATION },
    );
  if (kit === '')
    add(
      'SRC_VM_NAME_UNUSABLE',
      `The migration kit keeps no character of the name ${pyRepr(vm.name)} - only Latin letters, digits, '_' and '-' after its transliteration - so it would create the VM's server, ports and volumes without a name.`,
    );
  const multi = vm.disks.filter((d) => d.multiattach).map((d) => d.name || d.id);
  if (multi.length) add('SRC_VM_MULTIATTACH', `Multi-attach disk(s): ${pyNames(multi)}.`);
  // the boot disk: the first bootable disk, else the first, as the API's VMRef.root_disk
  const root = vm.disks.find((d) => d.bootable) ?? vm.disks[0];
  if (root && (root.kind === 'image_root' || root.kind === 'ephemeral')) add('SRC_VM_EPHEMERAL_ROOT', `The root disk is ${root.kind.replaceAll('_', ' ')}.`);
  // the VM's place in the source's Heat stacks and its Octavia memberships, as the API's (SDD §9.3, §10, plan task N2)
  const at = context.source ?? 'the source';
  if (context.stacks) for (const [code, message, own] of stackFindings(vm, plan, context.stacks, at)) add(code, message, own);
  if (context.lbMembers) for (const [code, message] of memberFindings(vm, plan, context.lbMembers, at)) add(code, message);
  // a network RHOSO lacks, as the API words it and lists it - before the networks it resolves (SDD §9.3, review RV25-03):
  // a NIC its override sends elsewhere is checked by
  // MAP_NIC_OVERRIDE_INVALID; pre-staging creates same-named networks for OpenStack sources only - the VMware kit has
  // no pre-stage step (§4.2, §7.2); nothing creates a network under a mapping's target name
  const has = (names: Record<string, unknown>, name: string) => Object.prototype.hasOwnProperty.call(names, name);
  const mappedNics = vm.nics.filter((nic) => !nicOverride(plan, vm.source_id, nic.mac)?.network);
  const missingNets = mappedNics
    .filter((nic) => !has(plan.mappings.networks, nic.network) && !has(destinationNetworks, nic.network))
    .map((nic) => nic.network);
  const badNetTargets = mappedNics
    .filter((nic) => has(plan.mappings.networks, nic.network) && !has(destinationNetworks, plan.mappings.networks[nic.network]!))
    .map((nic) => `${nic.network} -> ${plan.mappings.networks[nic.network]}`);
  const prestaged = plan.prestage_resources.includes('networks') && sourceKind !== 'vmware';
  if (missingNets.length && prestaged)
    add('MAP_NETWORK_PRESTAGED', `Pre-staging creates same-named network(s) in ${where}: ${pyNames(missingNets)}.`);
  else if (missingNets.length)
    add('MAP_NETWORK_MISSING', `No mapping or same-named network in ${where} for: ${pyNames(missingNets)}.` +
        (sourceKind === 'vmware' ? ' VMware sources are not pre-staged: map every port group.' : ''));
  if (badNetTargets.length)
    add('MAP_NETWORK_MISSING', `Network mapping target(s) do not exist in ${where}: ${pyNames(badNetTargets)}.`, { remediation: 'Fix the mapping: the destination network must exist.' });
  // every NIC's RHOSO network as the cutover resolves its name (SDD §7.3 step 0, §9.3); a VMware NIC's too: Seamless gives
  // the migration kit the network it resolves (§7.2)
  const seen = networkList({ networks: destinationNetworks, network_details: context.networkDetails });
  const resolved = new Map<Nic, DestinationNetwork>();
  const ambiguous: string[] = [];
  for (const nic of vm.nics) {
    // the VMware kit keeps the guest's network settings: no NIC override moves its NICs (SDD §7.2)
    const target = sourceKind === 'vmware' ? mappedNetwork(plan.mappings.networks, nic.network) : nicTarget(plan, vm.source_id, nic).network;
    const { network, why } = resolveNetwork(seen, target);
    if (network) resolved.set(nic, network);
    else if (why) ambiguous.push(`${(nic.mac ?? '?').toLowerCase()} (${nic.network}): ${why}`);
  }
  if (ambiguous.length)
    add('MAP_NETWORK_AMBIGUOUS', `The cutover cannot tell these networks of ${where} apart: ${ambiguous.join('; ')}.`);
  // whether the credential may put each NIC's port on its resolved network, as the API (SDD §6.6, §9.3); the VMware
  // migration kit creates every port with the NIC's MAC (§7.2)
  const denied: string[] = [];
  for (const nic of vm.nics) {
    const network = resolved.get(nic);
    const why = network ? portRefusal(network, context.roles, context.profile, where) : null;
    if (network && why) denied.push(`${(nic.mac ?? '?').toLowerCase()} on ${pyRepr(network.name)} (project ${network.project_id ?? 'None'}): ${why}`);
  }
  if (denied.length)
    add('NET_PORT_NOT_PERMITTED', `The credential of ${where} may not put these NICs on their network: ${denied.join('; ')}.`, { remediation: portRemediation(context.profile) });
  // one finding for every NIC whose network is smaller, worded as the API's (SDD §9.3, §16, review RV20-05)
  const shrinks: string[] = [];
  for (const nic of vm.nics) {
    const target = nicTarget(plan, vm.source_id, nic).network;
    const dstMtu = resolved.has(nic) ? resolved.get(nic)!.mtu : has(destinationNetworks, target) ? destinationNetworks[target] : undefined;
    if (nic.mtu && dstMtu && dstMtu < nic.mtu)
      shrinks.push(target === mappedNetwork(plan.mappings.networks, nic.network) ? `${nic.network} ${nic.mtu} -> ${dstMtu}` : `${nic.network} ${nic.mtu} -> ${target} ${dstMtu}`);
  }
  if (shrinks.length)
    add('NET_MTU_SHRINK', `The MTU in ${where} is smaller: ${pyNames(shrinks)}.`);
  const sriov = vm.nics.filter((nic) => ['direct', 'direct-physical', 'macvtap'].includes(nic.vnic_type)).map((nic) => nic.network);
  if (sriov.length) add('NET_SRIOV_PORT', `SR-IOV/macvtap port(s) on: ${pyNames(sriov)}.`);
  const oddPorts = portsWithoutOneFixedIp(withPlannedAddresses(plan, vm));
  if (sourceKind !== 'vmware' && oddPorts.length && plan.port_creation_mode !== 'neutron')
    add('NET_PORT_IPS', `NIC(s) without exactly one fixed IP: ${describePorts(oddPorts)}. Cold and warm migration cannot recreate them; storage handover keeps one address per port.`);
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
        if (why) refused.push(`${(nic.mac ?? '?').toLowerCase()} on ${pyRepr(network.name)}: ${address} ${why}`);
      }
    }
    if (refused.length)
      add('NET_ADDRESS_REFUSED', `Neutron would refuse these addresses on their network in ${where}: ${refused.join('; ')}.`);
  }
  // the VMware migration kit sends a NIC's IPv4 guest addresses only, all with one subnet of its network (SDD §7.2, §9.3)
  if (sourceKind === 'vmware' && context.subnets?.length) {
    const refused: string[] = [];
    for (const nic of vm.nics) {
      const network = resolved.get(nic);
      const why = network ? kitSubnet(nic, subnetsOf(context.subnets, network)).why : null;
      if (network && why) refused.push(`${(nic.mac ?? '?').toLowerCase()} on ${pyRepr(network.name)}: ${why}`);
    }
    if (refused.length)
      add('NET_ADDRESS_REFUSED', `Neutron would refuse these addresses on their network in ${where}: ${refused.join('; ')}.`);
  }
  for (const [code, message] of nicOverrideFindings(vm, sourceKind, plan, destinationNetworks, context))
    add(code, message);
  // what the VM's ports carry that the migration does not, and a network the waves split, as the API's (plan task N2b)
  if (sourceKind !== 'vmware')
    for (const [code, message] of portFindings(vm, plan, context.floatingIps ?? [], context.dns ?? { extension: null, zones: null }, at, where)) add(code, message);
  for (const [code, message] of prefixSplit(vm, plan, context.selected ?? [vm], context.announcers ?? [])) add(code, message);
  // an Octavia pool whose members the plan's waves split, as the API's (SDD §9.3, §23.7)
  if (context.lbMembers) for (const [code, message] of poolSplit(vm, plan, context.lbMembers, context.selected ?? [vm], at)) add(code, message);
  // the API's _gpu_blockers (SDD §9.3, §22.4, §22.5, plan task G4): a GPU VM no GPU mapping applies to - an OpenStack
  // flavor's requests named by their extra specs, a VMware VM's devices by what names them - and why none applies; its
  // GPUs as the API's _vm_gpus reads them, with the profile its source declares for its flavor (PR #64 review)
  const declared = declaredProfileOf(vm, context.gpuProfiles);
  const read = vmGpus(vm, declared);
  const gpuFlavors = context.gpuFlavors ?? [];
  const mapping = read.length ? gpuMapping(vm, read, sourceKind, plan, context.flavors ?? [], gpuFlavors, where) : { flavor: null, why: null };
  const why = mapping.why;
  // a mapped flavor the destination lacks is MAP_FLAVOR_MISSING's alone, as the API's (SDD §22.4)
  const missing = read.length && context.flavors ? gpuMissingTargets(vm, read, sourceKind, plan, context.flavors, where) : [];
  if (missing.length) add('MAP_FLAVOR_MISSING', missing.join(' '), { remediation: 'Fix the mapping: the destination flavor must exist.' });
  // each blocker by the GPUs read, never by an extra-spec key alone: an alias whose count asks for none is no request
  const alias = vm.flavor_extra_specs['pci_passthrough:alias'];
  const passthrough = read.filter((g) => g.kind === 'passthrough');
  if (why !== null && passthrough.length)
    add(
      'VM_PCI_PASSTHROUGH',
      `${
        alias !== undefined && passthrough.some((g) => g.alias)
          ? `Flavor requests PCI passthrough (${alias}).`
          : `The VM has a PCI passthrough device (${gpuNames(passthrough)}).`
      } ${why}`,
    );
  const vgpus = read.filter((g) => g.kind === 'vgpu');
  const keys = vgpuRequestKeys(vm.flavor_extra_specs, declared);
  if (why !== null && vgpus.length)
    add(
      'VM_VGPU',
      `${
        keys.length && vgpus.some((g) => g.resource_class)
          ? `Flavor requests a vGPU (${keys.join(', ')}).`
          : `The VM has a vGPU (${gpuNames(vgpus)}).`
      } ${why}`,
    );
  // the API's _gpu_findings (SDD §22.4, §22.5, plan task G12): a GPU VM told of its GPU flavor and any downsizing once its
  // mapping applies, every GPU VM of its guest's driver and a vGPU's license, and a VM whose server is created with a GPU
  // flavor - a VM without a GPU that a flavor mapping sends to one included - of the destination's capacity for it
  const gpuFindings: GpuFinding[] = [];
  let serverFlavor: GpuFlavor | null = mapping.flavor;
  if (read.length) {
    if (mapping.flavor) gpuFindings.push(...gpuFlavorFindings(vm, read, mapping.flavor, where));
    gpuFindings.push(...guestGpuFindings(read, mapping.flavor, sourceKind, where));
  } else {
    serverFlavor = serverGpuFlavor(vm, plan, gpuFlavors, sourceKind);
  }
  if (serverFlavor) {
    const capacity = context.gpuCapacity ?? { status: 'ok', reason: null, detail: null, read_at: null, pools: [] };
    gpuFindings.push(...capacityFindings(vm, serverFlavor, plan, gpuFlavors, capacity, sourceKind, where, context.selected ?? [vm], context.shares ?? {}));
  }
  for (const [code, message, own] of gpuFindings) add(code, message, own);
  const encrypted = vm.disks.filter((d) => d.encrypted).map((d) => d.name || d.id);
  if (encrypted.length)
    add('VOL_ENCRYPTED', `Encrypted disk(s): ${pyNames(encrypted)}.`);
  const guest = guestOsOf(vm);
  if (guest.lifecycle === 'legacy')
    add('GUEST_OS_LEGACY', `Guest OS ${guest.label} is out of standard vendor support.`);
  if (guest.family === 'unknown')
    add('GUEST_OS_UNKNOWN', `The guest OS is not identified${vm.os_type ? ` (${vm.os_type})` : ''}.`);
  // virt-v2v converts a VMware guest - the source's kind says so, or a VMDK disk does - as the API judges it
  const converted = sourceKind === 'vmware' || vm.disks.some((d) => d.kind === 'vmdk');
  if (converted && (guest.v2v === 'tech_preview' || guest.v2v === 'unverified'))
    add('GUEST_CONVERSION_UNVERIFIED', `Converting ${guest.label} with virt-v2v is ${guest.v2v === 'tech_preview' ? 'a Technology Preview' : 'not supported by Red Hat'}.`);
  if (converted && guest.v2v === 'unsupported')
    add('GUEST_CONVERSION_UNSUPPORTED', `virt-v2v on the RHEL 9 conversion host cannot prepare ${guest.label}.`);
  // by the VM's own fields, as the API's: only VMware's inventory sets them
  if (vm.cbt_enabled === false) add('VMW_CBT_DISABLED', 'Changed Block Tracking is disabled.');
  const independent = vm.disks.filter((d) => d.independent).map((d) => d.name || d.id);
  if (independent.length) add('VMW_INDEPENDENT_DISK', `Independent disk(s): ${pyNames(independent)}.`);
  if (vm.snapshot_count > 0) add('VMW_SNAPSHOTS_PRESENT', `The VM has ${vm.snapshot_count} snapshot(s).`);
  if (vm.tools_ok === false) add('VMW_TOOLS_MISSING', 'VMware Tools are not running.');
  // as the API words it (SDD §9.3, §16, review RV20-08): every volume whose type handover cannot map
  const unmappedTypes = vm.disks.filter((d) => d.kind === 'volume' && !Object.hasOwn(plan.handover.backend_map, d.volume_type || '')).map((d) => d.volume_type || '<default>');
  if (plan.handover.enabled && unmappedTypes.length)
    add('HANDOVER_BACKEND_UNMAPPED', `Volume type(s) without a handover backend: ${pyNames(unmappedTypes)}.`);
  // like the API (SDD §9.3, ADV-18): a database VM verified only by its boot
  if (isDatabase(vm) && probesFor(vm.source_id, plan).probes.length === 0) {
    const suggested = suggestProbes(vm);
    add('APP_PROBE_MISSING', `${vm.name} is a database VM without an application probe: verification would check only that it booted.${suggested.length ? ` Suggested: ${describeProbes(suggested)}.` : ''}`);
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
    extent_tracking: false,
    // the provider's own credentials (SDD §4.2, §27.4)
    source_project: null,
    destination_project: null,
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
    acknowledged_findings: {} as Plan['acknowledged_findings'],
    network_cutover: { hooks: {}, floating_ips: 'auto' as const, dns_targets: {}, lb_pools: {} },
    waves: [],
    // what the API derives from the migrations (SDD §8.1); the mock sets it on every response
    derived_status: 'draft' as const,
    migration_counts: {} as Plan['migration_counts'],
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
  /** What the last warm pass met that the cutover will refuse before the source stops (SDD §7.2). */
  refusals?: string[];
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
    // the demo's finance plan tracks its Ceph volumes' extents (SDD §28, plan task CE11)
    overrides: { extent_tracking: true,
      mappings: { networks: { 'tenant-infra': 'tenant-infra' }, flavors: { 'g1.a10': 'm1.large' }, volume_types: { 'tripleo-ceph': 'ceph-hdd', 'tripleo-ceph-ssd': 'ceph-ssd' }, projects: {} },
      // PRD G1: warm cutovers within 15 minutes.
      downtime_slo_s: 900,
      // the broker's autoscaling group is suspended at the source: the plan acknowledges its member (SDD §16, plan task N2)
      acknowledged_findings: { 'os-0b32': ['SRC_VM_AUTOSCALED'] },
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
        notes: [{ kind: 'classification', source: 'rules', summary: 'Classified as infrastructure_service: tag app=active-directory.', confidence: null, data: { tier: 'infrastructure_service' }, ago: 3 * D }],
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
        // a port another VM left behind holds the NIC's MAC: the pre-copy goes on, the cutover would refuse (SDD §7.2)
        refusals: ["the NIC 00:50:56:9a:51:01 of vm-erp-app-01 on 'VM Network ERP': port port-3c1d holds its MAC 00:50:56:9a:51:01"],
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
    // retired before any of its migrations started (SDD §12): its VMs went to "Community lab cleanup"
    id: 'plan-6a1b2c3d',
    name: 'Lab trial — k8s',
    description: 'A first try at the lab cloud, made before its source failed; cancelled.',
    source: 'community-lab',
    destination: 'rhoso-prod',
    status: 'cancelled',
    createdAgo: 5 * D,
    overrides: { require_approval: false },
    waves: [],
    migrations: [{ id: 'mig-c3d5e7f970', vm: 'lab-2b01', strategy: 'cold', phase: 'cancelled', enteredAgo: 4 * D }],
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
  /** The notification catalog, one channel of each kind (document kind `notification_channel`, SDD §24.3, §24.9). */
  channels: NotificationChannel[];
  /** Each channel's subscription to a plan or to every plan (document kind `notification_subscription`, SDD §24.4). */
  subscriptions: NotificationSubscription[];
  /** Each channel's place in the events and its delivery state (document kind `notification_cursor`, SDD §24.6). */
  cursors: NotificationCursor[];
}

export function buildFixtures(now: number): Fixtures {
  const providers = buildProviders(now);
  const inventories = buildInventories(now);
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

    const selected = plan.vm_ids.flatMap((id) => vms.filter((v) => v.source_id === id));
    // what a cutover of each of the plan's VMs will still create, by its migration's phase (SDD §9.3, §22.5)
    const shares = Object.fromEntries(spec.migrations.map((m) => [m.vm, quotaShare(m.phase)]));
    for (const m of spec.migrations) {
      const vm = vms.find((v) => v.source_id === m.vm);
      if (!vm) continue;
      const findings = preflight(vm, source.kind, plan, dstInventory.networks, {
        selected,
        destination: providerLabel(destination),
        // the source by its label, and its Heat and Octavia reads (SDD §9.3, §10, §16, plan task N2)
        source: providerLabel(source),
        ...sourceReads(source.id, vms),
        // the destination's floating IPs and DNS, and who announces tenant networks (SDD §9.3, §16, plan task N2b)
        ...networkContext(dstInventory, source, destination),
        ...flavorContext(dstInventory, destination.gpu_profiles, iso(now)),
        shares,
        gpuProfiles: source.gpu_profiles,
      });
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
      // a tracked plan's qualifying disks: the source cluster's Cinder RBD volumes, not encrypted (SDD §28.4, §28.6, plan task CE10)
      const clusterOf = new Map(
        ((source.capabilities.storage_backends as Array<{ pool?: unknown; family?: unknown; ceph_fsid?: unknown }> | undefined) ?? [])
          .filter((b) => b.family === 'rbd' && typeof b.pool === 'string')
          .map((b) => [b.pool as string, typeof b.ceph_fsid === 'string' ? b.ceph_fsid : null]),
      );
      const trackedDisks =
        plan.extent_tracking && source.ceph && m.strategy === 'warm'
          ? vm.disks.filter((d) => d.kind === 'volume' && !d.encrypted && d.pool && clusterOf.get(d.pool) === source.ceph!.fsid).map((d) => d.id)
          : [];
      // how a pass used each disk's extents, as the simulator records it (SDD §28.5): the first pass scans every disk, a
      // later one checks the tracked disks (the final pass reads them) and scans the others
      const extentUse = (kind: SyncPass['kind'], first: boolean): Record<string, ExtentUse> => {
        const uses: Record<string, ExtentUse> = {};
        if (!trackedDisks.length) return uses;
        for (const d of vm.disks) {
          const device = d.device ?? d.id;
          if (first) uses[device] = { mode: 'scanned', reason: 'first_pass', query_s: null, missed: 0, assumed_zero: false };
          else if (trackedDisks.includes(d.id)) uses[device] = { mode: kind === 'final' ? 'read' : 'checked', reason: null, query_s: kind === 'final' ? 8.4 : 9.6, missed: 0, assumed_zero: false };
          else uses[device] = { mode: 'scanned', reason: d.encrypted ? 'encrypted' : 'not_tracked', query_s: null, missed: 0, assumed_zero: false };
        }
        return uses;
      };
      const syncPasses: SyncPass[] = scheduled.map(({ spec: p, start, end }, i) => {
        const changed = Math.round(p.changedGiB * GiB);
        return {
          ...(trackedDisks.length && end !== null ? { extents: extentUse(p.kind, i === 0) } : {}),
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
        tracked_disks: trackedDisks,
        // a checked pass calibrates the query time (SDD §28.6)
        observed_extent_query_s: trackedDisks.length && syncPasses.some((p) => p.kind !== 'full' && p.kind !== 'final' && p.ended_at !== null) ? 9.6 : null,
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
        last_recommendation: null,
        converged_by: fixtureConvergedBy(m.strategy, m.phase, syncPasses, chosen, plan),
        force_cutover: null,
        data_issues: [],
        cutover_refusals: m.refusals ?? [],
        downtime_model_s: null,
        verification: fixtureVerification(m.phase, downtimeEndedAt, m.error ?? null),
        suggested_probes: m.phase === 'pending' ? [] : suggestProbes(vm),
        source_running_at: null,
        gpu_wait: null,
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
        const destination = providers.find((p) => p.id === plan.destination_provider_id);
        const snapshot = strategySnapshot({
          vm, plan, estimates, findings, scope: estimates.filter((e) => e.eligible).map((e) => e.strategy), deterministic: m.rec.deterministic, at: validated,
          destination: destination && destinationDescriptor(destination), destinationName: destination && providerLabel(destination),
        });
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
        migration.last_recommendation = decided.id;
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
    emit({ ts: provider.last_checked_at ?? iso(now), kind: 'provider.checked', plan_id: null, migration_id: null, actor: 'bayu', message: `provider ${provider.id}: ${provider.status}`, data: { provider_id: provider.id, status: provider.status, message: provider.status_message } });
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

  // one notification channel of each kind, each subscribed to a plan or to every plan, with the delivery state its cursor
  // recorded (SDD §16, §24.9); addresses under example.com, and a Slack channel holds no URL (its secret, SDD §24.3)
  const channelAt = iso(now - 9 * D * 1000);
  const channels: NotificationChannel[] = [
    { name: 'itsm-webhook', kind: 'webhook', description: 'The ITSM receiver, signed', recipients: [], url: 'https://itsm.example.com/hooks/seamless', redact_names: true, enabled: true, created_at: channelAt, updated_at: channelAt },
    { name: 'ops-mailbox', kind: 'email', description: 'The operations mailbox', recipients: ['ops@example.com', 'migration-team@example.com'], url: null, redact_names: false, enabled: true, created_at: channelAt, updated_at: channelAt },
    { name: 'ops-slack', kind: 'slack', description: 'Slack #migrations', recipients: [], url: null, redact_names: false, enabled: true, created_at: channelAt, updated_at: channelAt },
  ];
  const subscriptions: NotificationSubscription[] = [
    { id: 'sub-3a7f1c2e', channel: 'ops-mailbox', plan_id: null, topics: ['approval', 'downtime', 'problem'], created_by: 'rina', created_at: channelAt },
    { id: 'sub-5b2d9e04', channel: 'ops-slack', plan_id: 'plan-4f2a9c1e', topics: ['approval', 'downtime', 'problem'], created_by: 'bayu', created_at: iso(now - 8 * D * 1000) },
    { id: 'sub-8c1e4f6a', channel: 'itsm-webhook', plan_id: 'plan-2d5c8b93', topics: ['outcome', 'problem'], created_by: 'bayu', created_at: iso(now - 8 * D * 1000) },
  ];
  const last = events.length;
  const cursors: NotificationCursor[] = [
    { channel: 'itsm-webhook', seq: last, last_outcome: 'failed', last_outcome_at: iso(now - 25 * 60_000), last_reason: 'HTTP 503', failures_in_a_row: 2 },
    { channel: 'ops-mailbox', seq: last, last_outcome: 'sent', last_outcome_at: iso(now - 20 * 60_000), last_reason: null, failures_in_a_row: 0 },
    { channel: 'ops-slack', seq: last, last_outcome: 'sent', last_outcome_at: iso(now - 20 * 60_000), last_reason: null, failures_in_a_row: 0 },
  ];
  return { providers, inventories, plans, migrations, recommendations, snapshots, policies, events, channels, subscriptions, cursors };
}
