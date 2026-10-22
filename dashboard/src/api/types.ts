/**
 * Wire types for the Seamless Migrate control-plane API.
 *
 * Mirrors SDD §4 (domain model) and §12 (REST API) exactly: JSON field names are the Python
 * attribute names, timestamps are ISO-8601 UTC strings ending in `Z`, sizes are integer bytes
 * unless the name ends in `_gb`, durations are float seconds (`_s`).
 */

/** ISO-8601 UTC timestamp, e.g. `2026-10-08T12:00:00Z`. */
export type Timestamp = string;

// ---------------------------------------------------------------------------------------------
// §4.1 Enums
// ---------------------------------------------------------------------------------------------

export const PROVIDER_KINDS = ['openstack', 'vmware', 'rhoso'] as const;
export type ProviderKind = (typeof PROVIDER_KINDS)[number];

export const PROVIDER_ROLES = ['source', 'destination'] as const;
export type ProviderRole = (typeof PROVIDER_ROLES)[number];

export const STRATEGIES = ['cold', 'warm', 'storage_handover', 'vmware_cold', 'vmware_warm'] as const;
export type Strategy = (typeof STRATEGIES)[number];

export const PHASES = [
  'pending',
  'validating',
  'blocked',
  'ready',
  'precopy',
  'syncing',
  'awaiting_cutover',
  'cutover',
  'verifying',
  'completed',
  'finalized',
  'failed',
  'rolling_back',
  'rolled_back',
  'cancelled',
] as const;
export type Phase = (typeof PHASES)[number];

export const SEVERITIES = ['blocker', 'warning', 'info'] as const;
export type Severity = (typeof SEVERITIES)[number];

/** Ordered: viewer < operator < approver < admin (SDD §13.2). */
export const ROLES = ['viewer', 'operator', 'approver', 'admin'] as const;
export type Role = (typeof ROLES)[number];

/** `cancelled` retires a plan none of whose migrations started (SDD §12): terminal, nothing deleted. */
export const PLAN_STATUSES = ['draft', 'validated', 'running', 'paused', 'completed', 'failed', 'cancelled'] as const;
export type PlanStatus = (typeof PLAN_STATUSES)[number];

/** The plan status every view shows (SDD §8.1, ADV-19): the lifecycle, told by the plan's migrations. */
export const DERIVED_PLAN_STATUSES = ['draft', 'validated', 'running', 'paused', 'completed', 'finalized', 'failed', 'cancelled'] as const;
export type DerivedPlanStatus = (typeof DERIVED_PLAN_STATUSES)[number];

export const SYNC_PASS_KINDS = ['full', 'delta', 'final'] as const;
export type SyncPassKind = (typeof SYNC_PASS_KINDS)[number];

export const PROVIDER_STATUSES = ['unknown', 'ok', 'degraded', 'error'] as const;
export type ProviderStatus = (typeof PROVIDER_STATUSES)[number];

export const DISK_KINDS = ['volume', 'ephemeral', 'image_root', 'vmdk'] as const;
export type DiskKind = (typeof DISK_KINDS)[number];

export const POWER_STATES = ['running', 'stopped', 'paused', 'error', 'transitioning', 'unknown'] as const;
export type PowerState = (typeof POWER_STATES)[number];

export const SELECTION_POLICIES = ['min_downtime', 'simplest_meeting_slo'] as const;
export type SelectionPolicy = (typeof SELECTION_POLICIES)[number];

export const ADVISOR_NOTE_KINDS = ['strategy', 'classification', 'verification', 'similar_incidents', 'screen'] as const;
export type AdvisorNoteKind = (typeof ADVISOR_NOTE_KINDS)[number];

export const ADVISOR_NOTE_SOURCES = ['jev', 'rules', 'memory'] as const;
export type AdvisorNoteSource = (typeof ADVISOR_NOTE_SOURCES)[number];

/** What an advisor recommendation decides (SDD §4.1, §14.4). */
export const DECISION_TYPES = ['migration_strategy', 'workload_tier', 'verification_review', 'console_screening'] as const;
export type DecisionType = (typeof DECISION_TYPES)[number];

export const DECISION_STATUSES = ['auto_applied', 'pending_approval', 'approved', 'overridden', 'below_threshold', 'fallback_rules'] as const;
export type DecisionStatus = (typeof DECISION_STATUSES)[number];

export const REASON_CODES = [
  'ABOVE_THRESHOLD_AUTO_APPLY',
  'POLICY_REQUIRES_APPROVAL',
  'INSUFFICIENT_EVIDENCE',
  'APPROVED_BY_USER',
  'OVERRIDDEN_BY_USER',
  'BELOW_THRESHOLD_DEFAULT_KEPT',
  'ADVISOR_UNAVAILABLE_FALLBACK',
] as const;
export type ReasonCode = (typeof REASON_CODES)[number];

/** Workload tiers used by the wave planner and advisor classification (SDD §9.4). */
export const WORKLOAD_TIERS = [
  'stateless_web',
  'middleware_queue',
  'infrastructure_service',
  'stateful_database',
  'legacy_os',
  'manual_review',
] as const;
export type WorkloadTier = (typeof WORKLOAD_TIERS)[number];

// ---------------------------------------------------------------------------------------------
// §4.2 Models
// ---------------------------------------------------------------------------------------------

export interface ConversionHostConfig {
  manage: boolean;
  name: string | null;
  flavor: string | null;
  external_network: string | null;
  image: string | null;
  ssh_user: string;
  address: string | null;
  /** CIDR allowed to SSH to the conversion host (Security.md SEC-03). */
  ssh_allowed_cidr: string | null;
  /** Secret holding the private key of an existing conversion host (VMware sources). */
  ssh_key_secret: string | null;
}

/** Presets and display only (SDD §4.2); `kind` decides the code path. */
export const DISTRIBUTIONS = ['openstack_community', 'kolla', 'rhosp', 'rhoso', 'vmware'] as const;
export type Distribution = (typeof DISTRIBUTIONS)[number];

export interface Provider {
  /** Regex `^[a-z0-9][a-z0-9-]{1,62}$`. */
  id: string;
  name: string;
  kind: ProviderKind;
  role: ProviderRole;
  endpoint: string;
  cloud: string | null;
  credentials_secret: string | null;
  region: string | null;
  verify_tls: boolean;
  ca_cert_path: string | null;
  conversion_host: ConversionHostConfig | null;
  capabilities: Record<string, unknown>;
  status: ProviderStatus;
  status_message: string | null;
  last_checked_at: Timestamp | null;
  distribution: Distribution | null;
  /** The community series its operator declares, e.g. "2026.2" (`^20[0-9]{2}\.[12]$`; SDD §4.2, §10.1); null = detected. Refused on vmware. */
  release: string | null;
  /** The policy defaults its operator declares (SDD §4.2, §21.5); null = by kind and series. Refused on vmware. */
  policy_profile: 'secure_rbac' | 'legacy' | null;
  /** The NVIDIA vGPU profile each of its GPU flavors gives, as its operator declares it (SDD §4.2, §22.3). Refused on vmware; older fixtures omit it. */
  gpu_profiles?: Record<string, string>;
  /** Its operator declares that the cloud's OVN BGP agent announces tenant networks, which no API shows: NET_PREFIX_SPLIT
   * (SDD §4.2, §9.3, §23.6). Refused on vmware; older fixtures omit it (false). */
  announces_tenant_networks?: boolean;
  /** Server-owned: when the write-only credentials / conversion key were last stored. */
  credentials_updated_at: Timestamp | null;
  conversion_key_updated_at: Timestamp | null;
}

/** `POST /providers` body (status and server-owned fields are ignored by the API). */
export type ProviderCreate = Pick<Provider, 'id' | 'name' | 'kind' | 'role' | 'endpoint' | 'cloud' | 'region' | 'verify_tls' | 'ca_cert_path' | 'conversion_host' | 'distribution' | 'credentials_secret'> &
  Partial<Pick<Provider, 'release' | 'policy_profile' | 'gpu_profiles' | 'announces_tenant_networks'>>;

/** `PATCH /providers/{id}`: the editable fields (SDD §12). */
export type ProviderPatch = Partial<
  Pick<Provider, 'name' | 'endpoint' | 'cloud' | 'credentials_secret' | 'region' | 'verify_tls' | 'ca_cert_path' | 'conversion_host' | 'distribution' | 'release' | 'policy_profile' | 'gpu_profiles' | 'announces_tenant_networks'>
>;

/** `PUT /providers/{id}/credentials`: write-only; never returned (SDD §13.3). */
export interface ProviderCredentials {
  auth_url?: string;
  username?: string;
  password?: string;
  project_name?: string;
  user_domain_name?: string;
  project_domain_name?: string;
  application_credential_id?: string;
  application_credential_secret?: string;
  interface?: 'public' | 'internal' | 'admin';
  datacenter?: string;
}

export interface Disk {
  id: string;
  name: string | null;
  size_gb: number;
  used_gb: number | null;
  bootable: boolean;
  volume_type: string | null;
  device: string | null;
  kind: DiskKind;
  multiattach: boolean;
  encrypted: boolean;
  independent: boolean;
  /** Cinder `host@backend#pool` of a volume (admin only; SDD §4.2, §7.3.1). */
  pool?: string | null;
}

/** Derived by the control plane from `os_type` (SDD §9.5). */
export interface GuestOS {
  family: 'linux' | 'windows' | 'unknown';
  distro: string | null;
  version: string | null;
  label: string;
  lifecycle: 'current' | 'legacy' | 'unknown';
  v2v: 'supported' | 'tech_preview' | 'unverified' | 'unsupported' | 'unknown';
}

export interface Nic {
  network: string;
  mac: string | null;
  fixed_ips: string[];
  vnic_type: string;
  mtu: number | null;
  /** OpenStack: the addresses of the port's allowed address pairs, which the import does not carry (SDD §4.2, §9.3); older
   * answers omit them. */
  allowed_address_pairs?: string[];
  /** OpenStack: the addresses of the floating IPs associated with the port (SDD §4.2, §9.3). */
  floating_ips?: string[];
  /** OpenStack: the port's DNS name and its domain - the port's, else its network's - under Neutron's DNS integration (SDD §4.2). */
  dns_name?: string | null;
  dns_domain?: string | null;
  /** VMware: what the migration kit maps the NIC by, its distributed port group's key or device summary (SDD §4, §7.2). */
  network_key?: string | null;
}

export interface VMRef {
  source_id: string;
  name: string;
  project: string | null;
  flavor: string | null;
  vcpus: number;
  ram_mb: number;
  disks: Disk[];
  nics: Nic[];
  power_state: PowerState;
  os_type: string | null;
  host: string | null;
  tags: Record<string, string>;
  flavor_extra_specs: Record<string, string>;
  cbt_enabled: boolean | null;
  snapshot_count: number;
  tools_ok: boolean | null;
  change_rate_bps: number | null;
  /** The task Nova runs on the server whatever its status (OS-EXT-STS:task_state), apart from `power_state`; null or
   *  absent for none, for VMware and from older control planes (SDD §4.2, review PV-05). */
  task_state?: string | null;
  /** The Nova keypair the server boots with (OpenStack); absent or null for VMware (SDD §4.1). */
  key_name?: string | null;
  /** Derived and serialized: Σ size_gb · 2^30. */
  disk_bytes: number;
  /** Derived and serialized: Σ used_gb · 2^30 when every disk has used_gb, else ⌊disk_bytes · 0.6⌋. */
  used_bytes: number;
  /** Derived and serialized from `os_type` (SDD §9.5); absent in responses of older control planes. */
  guest_os?: GuestOS;
  /** Its GPUs as its source reports them (SDD §4.2, §22.2, plan task G2); [] for a VM recorded before, absent from older control planes. */
  gpus?: GpuDevice[];
}

/**
 * A GPU of a source VM, or a GPU request of a flavor (SDD §4.2, §22.2): a VMware vGPU by its profile or a passthrough
 * device by its PCI ids; an OpenStack request by its resource class, traits and PCI alias. The API sends every field,
 * null or [] when unset.
 */
export interface GpuDevice {
  kind: 'vgpu' | 'passthrough';
  /** The vGPU profile: VMware's as vCenter reports it ("grid_a100-4c"), an OpenStack request's as its provider declares it. */
  profile?: string | null;
  /** VMware passthrough: the PCI vendor and device ids, four lower-case hex digits each ("10de", "20b5"). */
  vendor_id?: string | null;
  device_id?: string | null;
  /** Identical devices of one VM are one entry. */
  count: number;
  /** OpenStack vGPU: the request group's class ("VGPU", or a CUSTOM_ class) with its required and forbidden traits. */
  resource_class?: string | null;
  traits?: string[];
  forbidden_traits?: string[];
  /**
   * OpenStack vGPU: a numbered request group's (`resources1:`), whose traits Placement checks on the GPU's own provider;
   * the unnumbered group's it checks across the GPU's provider and its compute node (SDD §22.3). Older VMs omit it.
   */
  same_provider?: boolean;
  /** OpenStack passthrough: the PCI alias ("a100"). */
  alias?: string | null;
}

/** A Placement pool of a destination's GPU requests: what a request asks Placement for, and counts only (SDD §22.3). */
export interface GpuPool {
  resource_class: string;
  traits: string[];
  forbidden_traits: string[];
  /** Its traits are a numbered request group's, held by the GPU's own provider; false for the unnumbered group's and a class alone. */
  same_provider: boolean;
  /** The units free now on the providers that hold the class and the traits. */
  free: number;
  /** The resource providers among them with at least one unit free. */
  providers: number;
  /** The most units free on one of them: below n when no provider can give a group of n units. */
  largest_free: number;
  /** Placement refused the pool's request as a bad one (400): its HTTP status and error code; the counts are then 0. */
  detail: string | null;
}

/**
 * A destination's GPU capacity as Placement shows it, a key of its check report read when the check runs (SDD §10,
 * §22.3). Unknown when Placement refused the credential, the catalog has no Placement, or another failure; `detail`
 * names the failure by its HTTP status and Placement error code, never by its message text (§22.12).
 */
export interface GpuCapacity {
  status: 'ok' | 'unknown';
  reason: 'forbidden' | 'no_placement' | 'error' | null;
  detail: string | null;
  read_at: Timestamp | null;
  pools: GpuPool[];
}

/**
 * What §22 adds to a destination's check report (`Provider.capabilities`, SDD §10, §22.3): its GPU capacity, read when
 * the check runs. A source's report holds none, nor does a report from before 0.3.0.
 */
export interface GpuCheckReport {
  gpu_capacity?: GpuCapacity;
}

export interface Mappings {
  networks: Record<string, string>;
  flavors: Record<string, string>;
  volume_types: Record<string, string>;
  projects: Record<string, string>;
  /** A VMware GPU key to a destination GPU flavor (SDD §4.2, §22.4); older fixtures omit it. */
  gpus?: Record<string, string>;
}

export interface HandoverConfig {
  enabled: boolean;
  /** source volume_type -> RHOSO cinder host "hostgroup@backend#pool", or "hostgroup@backend" to resolve the pool per volume (SDD §7.3.1). */
  backend_map: Record<string, string>;
}

export interface CutoverWindow {
  start: Timestamp;
  end: Timestamp;
}

export const PROBE_KINDS = ['tcp', 'http', 'https'] as const;
export type ProbeKind = (typeof PROBE_KINDS)[number];

/** An application probe (SDD §7.5, ADV-18): a TCP connect, or an HTTP(S) GET of `path`. */
export interface AppProbe {
  kind: ProbeKind;
  port: number;
  /** http(s) only. */
  path: string;
  /** http(s) only: the status that passes; null = any 2xx or 3xx. */
  expect_status: number | null;
}

/** What one application probe found (SDD §7.5): unreachable does not fail the verification. */
export interface ProbeResult {
  target: string;
  kind: ProbeKind;
  result: 'passed' | 'failed' | 'unreachable';
  /** The time to the service's answer; null when it did not answer. */
  latency_ms: number | null;
  at: Timestamp;
  detail: string;
}

export interface VerificationCheck {
  name: string;
  ok: boolean;
  detail: string;
  skipped?: boolean;
}

/** A migration's last verification: its checks and probe results (SDD §7.5). */
export interface VerificationRecord {
  at: Timestamp;
  passed: boolean;
  checks: VerificationCheck[];
  probes: ProbeResult[];
  warnings: string[];
}

/** What one probe list may hold, and the longest http(s) path (SDD §12). */
export const MAX_PROBES_PER_VM = 10;
export const MAX_PROBE_PATH = 200;

export interface VerificationConfig {
  tcp_ports: number[];
  /** Probed instead of `tcp_ports` for Windows guests, whose console check is skipped (SDD §7.5). */
  windows_tcp_ports: number[];
  probe_address: 'fixed' | 'floating';
  console_success_patterns: string[];
  timeout_s: number;
  auto_rollback: boolean;
  use_advisor: boolean;
  /** The application probes of every VM; `Plan.probe_overrides` replaces them per VM (SDD §7.5, ADV-18). */
  app_probes: AppProbe[];
}

export interface Wave {
  /** `wave-<n>`. */
  id: string;
  name: string;
  order: number;
  vm_ids: string[];
  depends_on: string[];
  max_parallel: number;
}

export type PortCreationMode = 'nova' | 'neutron';

/** A VM NIC's network and addresses at RHOSO (SDD §4.2): null keeps the plan's mapping, or the NIC's own addresses. */
export interface NicOverride {
  network: string | null;
  /** Never empty. */
  fixed_ips: string[] | null;
}

/** Where a migration runs a plan's hooks (SDD §4.2, §23.3). */
export type HookPoint = 'pre_stop' | 'post_create' | 'post_verify' | 'on_rollback' | 'on_finalize';

/** Where a VM's new address goes in DNS (SDD §4.2, §23.5): rfc2136 needs a server and a TSIG secret, designate takes neither. */
export interface DnsTarget {
  kind: 'designate' | 'rfc2136';
  zone: string;
  record: string;
  server: string | null;
  tsig_secret: string | null;
}

/** A plan's network cut-over settings (SDD §4.2, plan task N3). */
export interface NetworkCutover {
  /** Per point, the names of catalog hooks in their run order, each once per point. */
  hooks: Partial<Record<HookPoint, string[]>>;
  /** `keep_address`: the move that keeps a floating IP's address (SDD §23.4); `auto`: os-migrate's. */
  floating_ips: 'auto' | 'keep_address';
  /** Per VM (source id). */
  dns_targets: Record<string, DnsTarget>;
  /** A source Octavia pool (its id) to a destination pool (its id) (SDD §23.7). */
  lb_pools: Record<string, string>;
}

/** A hook of the catalog an admin keeps (SDD §4.2, §23.3). */
export interface Hook {
  name: string;
  kind: 'playbook' | 'webhook';
  description: string | null;
  playbook: string | null;
  url: string | null;
  secret: string | null;
  timeout_s: number;
  revert: string | null;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface Plan {
  /** `plan-<8 hex>`. */
  id: string;
  name: string;
  description: string | null;
  source_provider_id: string;
  destination_provider_id: string;
  vm_ids: string[];
  mappings: Mappings;
  default_strategy: Strategy | 'auto';
  strategy_overrides: Record<string, Strategy>;
  selection_policy: SelectionPolicy;
  downtime_slo_s: number;
  require_approval: boolean;
  /** Maker-checker (SDD §5.4, ADV-17): the cutover's requester never counts as its approver. */
  require_distinct_approver: boolean;
  auto_cutover: boolean;
  cutover_window: CutoverWindow | null;
  keep_warm_interval_s: number;
  convergence_threshold_bytes: number;
  max_sync_passes: number;
  link_bps: number;
  /** Overrides of the estimator parameters for this plan (SDD §9.1), e.g. `{scan_bps, parallel_disks}`. */
  estimator_overrides: Record<string, number>;
  handover: HandoverConfig;
  verification: VerificationConfig;
  /** Per VM (source id): its application probes instead of `verification.app_probes`; [] = none (SDD §7.5). */
  probe_overrides: Record<string, AppProbe[]>;
  /** Hours a migration may wait in Completed before it is flagged for finalize (SDD §8.1, ADV-19). */
  finalize_warning_h: number;
  prestage_resources: string[];
  /**
   * `neutron`: OpenStack cold and warm migrations recreate every port with its MAC, all its fixed IPs and its security
   * groups (SDD §6.6); `nova`: one fixed IP per port and a new MAC.
   */
  port_creation_mode: PortCreationMode;
  /** Per VM (source id), per NIC MAC (lower case): its RHOSO network and addresses; OpenStack sources (SDD §4.2). */
  nic_overrides: Record<string, Record<string, NicOverride>>;
  /**
   * Per VM (source id): the pre-flight codes the plan acknowledges for it - SRC_VM_AUTOSCALED alone, else 422; an
   * acknowledged blocker stays a finding, a warning that says so (SDD §4.2, §9.3, plan task N2).
   */
  acknowledged_findings: Record<string, string[]>;
  /** The hooks it runs per point, the floating IP move, DNS targets and Octavia pool mappings (SDD §4.2, §23.3-§23.7). */
  network_cutover: NetworkCutover;
  waves: Wave[];
  status: PlanStatus;
  /** Read-only: from the plan and its migrations (SDD §8.1); every view shows it, never `status`. */
  derived_status: DerivedPlanStatus;
  /** Read-only: how many of the plan's migrations are in each phase that has any; {} before the first validation (SDD §4, §8.1). */
  migration_counts: Partial<Record<Phase, number>>;
  created_at: Timestamp;
  updated_at: Timestamp;
}

export interface SyncPass {
  /** 1-based. */
  number: number;
  kind: SyncPassKind;
  started_at: Timestamp;
  ended_at: Timestamp | null;
  bytes_scanned: number;
  bytes_changed: number;
  bytes_transferred: number;
  duration_s: number | null;
}

export interface Finding {
  code: string;
  severity: Severity;
  message: string;
  remediation: string | null;
  /** Empty = applies to every strategy. */
  strategies: Strategy[];
}

export interface Estimate {
  strategy: Strategy;
  eligible: boolean;
  reasons: string[];
  precopy_s: number;
  passes: number;
  downtime_s: number;
  total_s: number;
  final_delta_bytes: number;
  meets_slo: boolean;
  /** The downtime's parts (SDD §4, ADV-16); empty in an estimate written before them. */
  downtime_parts: Partial<Record<DowntimePart, number>>;
}

/** The parts of a downtime estimate, in display order (SDD §4, §9.1, ADV-16): they add up to `downtime_s`. */
export const DOWNTIME_PARTS = ['shutdown_s', 'copy_s', 'conversion_s', 'create_s', 'boot_s'] as const;
export type DowntimePart = (typeof DOWNTIME_PARTS)[number];

export interface AdvisorNote {
  kind: AdvisorNoteKind;
  source: AdvisorNoteSource;
  summary: string;
  confidence: number | null;
  data: Record<string, unknown>;
  created_at: Timestamp;
}

/** Who took a decision: `policy:<id>`, `user:<name>` or `system:fallback` (SDD §14.4). */
export interface AdvisorActor {
  kind: 'policy' | 'user' | 'system';
  id: string;
}

export interface AdvisorModelInfo {
  name: 'jev' | 'rules';
  version: string;
  deployment: 'on_prem' | 'remote' | 'unknown';
  endpoint_host: string | null;
}

export interface RecommendedOption {
  option: string | null;
  /** 0..1 — the only number the control plane compares with a threshold; the dashboard never does (SDD §14.4). */
  confidence: number | null;
  /** Raw probabilities per option, shown only in Details. */
  distribution: Record<string, number>;
  /** Why the answer could not be applied when it was recorded, else null: no approve takes it (SDD §4.2, §14.6). */
  unusable: string | null;
}

export interface Decision {
  final_option: string | null;
  status: DecisionStatus;
  reason_code: ReasonCode;
  reason_text: string | null;
  actor: AdvisorActor;
  policy_id: string | null;
  threshold_used: number | null;
  decided_at: Timestamp;
  /** The deciding policy's rules for a person's approve or override, as they were then (SDD §14.6); null: the policy in force applies. */
  require_distinct_approver?: boolean | null;
  require_override_reason?: boolean | null;
}

export interface AdvisorSubject {
  kind: 'vm' | 'plan' | 'wave';
  id: string;
  name: string;
}

export const EVIDENCE_EFFECTS = ['supports', 'opposes', 'context'] as const;
export type EvidenceEffect = (typeof EVIDENCE_EFFECTS)[number];
export const EVIDENCE_SOURCE_KINDS = ['inventory', 'preflight', 'estimate', 'plan', 'provider', 'run'] as const;
export type EvidenceSourceKind = (typeof EVIDENCE_SOURCE_KINDS)[number];

/** One fact a recommendation rests on, built by the control plane's code, never by the model (SDD §14.4, ADV-02). */
export interface EvidenceItem {
  key: string;
  label: string;
  value: string | number | boolean | null;
  display: string;
  /** Measured against the recommendation's `evidence_option`. */
  effect: EvidenceEffect;
  /** Relevance, 0..100: the card shows the heaviest. */
  weight: number;
  /** The VM (inventory), the migration (preflight, estimate, run), the plan or the provider. */
  source: { kind: EvidenceSourceKind; ref_id: string };
}

/** A past incident agentmemory knew when the recommendation was recorded (SDD §14.3). */
export interface SimilarIncident {
  title: string;
  content: string;
  score: number | null;
}

/** A reason Jev gave that names one of the recommendation's evidence items (SDD §14.4). */
export interface AdvisorReason {
  key: string;
  text: string;
}

/** A recommendation and the decision taken on it, stored apart (SDD §4.2, §14.4). The engine runs only the decision. */
export interface AdvisorRecommendation {
  id: string;
  created_at: Timestamp;
  decision_type: DecisionType;
  subject: AdvisorSubject;
  plan_id: string | null;
  migration_id: string | null;
  /** The plan's source provider. */
  provider_id: string | null;
  model: AdvisorModelInfo;
  recommendation: RecommendedOption;
  decision: Decision;
  /** The redacted input the recommendation rests on, an `AdvisorSnapshot` (SDD §14.5). */
  input_snapshot_sha256: string | null;
  /** The user whose plan validation recorded it: never the one who decides it (SDD §14.6). */
  requested_by: string | null;
  /** A strategy note written before recommendations existed, read as one (SDD §14.4). */
  legacy: boolean;
  legacy_text: string | null;
  /** The option every evidence effect is measured against (SDD §14.4, ADV-02). */
  evidence_option: string | null;
  /** What validation knew when it was recorded, heaviest first. */
  evidence: EvidenceItem[];
  /** At most 3, searched once when it was recorded. */
  similar_incidents: SimilarIncident[];
  /** Jev's reasons that name an evidence key. */
  reasons: AdvisorReason[];
}

/** Where a policy applies: plans or source providers; neither = everywhere (SDD §14.6). */
export interface PolicyScope {
  provider_ids: string[];
  plan_ids: string[];
}

export const POLICY_MODES = ['auto_apply_above_threshold', 'always_require_approval'] as const;
export type PolicyMode = (typeof POLICY_MODES)[number];
/** A policy name is 1–200 characters and its minimum evidence 0..100 (SDD §4). */
export const POLICY_NAME_MAX = 200;
export const POLICY_MIN_EVIDENCE_MAX = 100;

/** A policy as it is written: POST and PUT /advisor/policies (SDD §12, §14.6). */
export interface AdvisorPolicySpec {
  name: string;
  scope: PolicyScope;
  /** None = every decision type. */
  decision_types: DecisionType[];
  mode: PolicyMode;
  /** 0..1 */
  threshold: number;
  min_evidence_for_auto_apply: number;
  require_override_reason: boolean;
  /** Maker-checker (ADV-17): the decision's requester neither approves nor overrides it. */
  require_distinct_approver: boolean;
}

export interface AdvisorPolicy extends AdvisorPolicySpec {
  id: string;
  created_at: Timestamp;
  updated_at: Timestamp;
  updated_by: string;
}

/** GET /advisor/policies: the stored policies and the built-in default, read-only (SDD §12). */
export interface PolicyList {
  items: AdvisorPolicy[];
  default: AdvisorPolicy;
}

/** POST /advisor/recommendations/{id}/override (SDD §12, §14.6). */
export interface OverrideRequest {
  option: Strategy;
  reason?: string | null;
}

/** The shortest override reason a policy that requires one accepts (SDD §14.6). */
export const OVERRIDE_REASON_MIN = 10;

/** A redacted advisor input, stored once under the SHA-256 of its canonical JSON (SDD §14.5). */
export interface AdvisorSnapshot {
  sha256: string;
  created_at: Timestamp;
  payload: Record<string, unknown>;
}

export interface Approval {
  actor: string;
  at: Timestamp;
  comment: string | null;
}

/** A cutover forced before convergence (SDD §5.4, ADV-16). */
export interface ForceCutover {
  actor: string;
  at: Timestamp;
  reason: string;
  /** The last pass's `bytes_changed` when the cutover was forced. */
  delta_bytes: number;
  estimated_downtime_s: number;
}

/** The rule that ended the passes (SDD §5.3); `force` for a Force cutover (§5.4). */
export const CONVERGENCE_RULES = ['threshold', 'slo', 'max_passes', 'force'] as const;
export type ConvergedBy = (typeof CONVERGENCE_RULES)[number];

export interface PhaseChange {
  from_phase: Phase | null;
  to_phase: Phase;
  at: Timestamp;
  reason: string;
  actor: string;
}

/**
 * A cutover that waits for GPU capacity, its source VM running (SDD §4.2, §5.4 rule 5, §22.7): what the last capacity
 * read found short, or that it failed, since when.
 */
export interface GpuWait {
  /** When the destination answered the read that began the wait. */
  since: Timestamp;
  /** When the destination answered the last read. */
  checked_at: Timestamp;
  /**
   * What that read found short, by pool name: a pool - its class, its required traits, then the forbidden ones prefixed
   * with "!" ("VGPU CUSTOM_NVIDIA_A100_4C") - or a class alone across its pools; "" while no read of the wait could
   * list the GPU flavor's requests.
   */
  pool: string;
  /**
   * The units the cutover needs there: its GPU flavor's, plus those of the destination's other migrations in cutover
   * with no server yet; for a request group of n > 1 units, the n one provider must have free; 0 with pool "".
   */
  needed: number;
  /** The units the read found free there (the most on one provider, for such a group); null when the read failed. */
  free: number | null;
  /** Why, in words, naming the destination by its label; a failure by its HTTP status, Placement error code or kind. */
  reason: string;
}

export interface Migration {
  /** `mig-<10 hex>`. */
  id: string;
  plan_id: string;
  wave_id: string | null;
  vm: VMRef;
  strategy: Strategy;
  phase: Phase;
  phase_history: PhaseChange[];
  progress_pct: number;
  bytes_total: number;
  bytes_transferred: number;
  sync_passes: SyncPass[];
  /** Bytes transferred by the passes dropped from `sync_passes` (SDD §5.4); `bytes_transferred` counts them. */
  sync_bytes_dropped: number;
  estimate: Estimate | null;
  estimates: Estimate[];
  /** Per-stream scan throughput measured by the last delta pass (SDD §9.1 calibration), null before it. */
  observed_scan_bps: number | null;
  /** Mappings matched automatically by pre-flight, e.g. the smallest fitting flavor (`MAP_FLAVOR_AUTO`). */
  resolved_mappings: Mappings;
  findings: Finding[];
  checkpoint: string | null;
  downtime_started_at: Timestamp | null;
  downtime_ended_at: Timestamp | null;
  actual_downtime_s: number | null;
  approvals: Approval[];
  cutover_requested: boolean;
  /** Who asked for the cutover first, and when (SDD §5.4). */
  cutover_requested_by: string | null;
  cutover_requested_at: Timestamp | null;
  force_window: boolean;
  advisor_notes: AdvisorNote[];
  /** The recommendation whose decision waits for approval: the migration does not start meanwhile (SDD §5.4, §14.6). */
  advisor_hold: string | null;
  /**
   * The recommendation its last validation stored: what a decision acts on while no hold names one, whatever its time
   * (SDD §14.6, review RV51-08); null when none was stored since the field.
   */
  last_recommendation: string | null;
  /** The rule that ended the passes (SDD §5.3); null before convergence and in data written before ADV-16. */
  converged_by: ConvergedBy | null;
  force_cutover: ForceCutover | null;
  /** What the invariants found when it completed, one "CODE: text" each (SDD §5.5). */
  data_issues: string[];
  /**
   * What the last warm pass met that the cutover refuses before the source stops, one message each (SDD §7.2):
   * each pre-copy or sync pass replaces it, a successful cutover empties it.
   */
  cutover_refusals: string[];
  /** Demo: the model seconds the simulated steps kept the source stopped, summed while the clock is open (SDD §5.2). */
  downtime_model_s: number | null;
  /** The last verification: its checks and application probe results (SDD §7.5, ADV-18). */
  verification: VerificationRecord | null;
  /** The rules' probe suggestion at validation (SDD §9.6): runs only once added to the plan. */
  suggested_probes: AppProbe[];
  /** Completed, not finalized: when its source VM was seen running again (SDD §8.1, ADV-19). */
  source_running_at: Timestamp | null;
  /**
   * In ready, awaiting_cutover or a keep-warm pass: its cutover waits for GPU capacity (SDD §5.4 rule 5, §22.7); leaving
   * those phases drops it.
   */
  gpu_wait: GpuWait | null;
  review_required: boolean;
  review_reason: string | null;
  destination_server_id: string | null;
  error: string | null;
  attempts: number;
  /** Each retried attempt's strategy, oldest first: the evidence names them (SDD §4.2, §14.4). */
  attempt_strategies: Strategy[];
  created_at: Timestamp;
  updated_at: Timestamp;
}

/** The fields of a migration a {@link MigrationSummary} keeps, as the migration has them (SDD §4). */
export const MIGRATION_SUMMARY_FIELDS = [
  'id',
  'plan_id',
  'wave_id',
  'vm',
  'strategy',
  'phase',
  'phase_since',
  'preflight_ran',
  'eligible_strategies',
  'progress_pct',
  'estimate',
  'actual_downtime_s',
  'downtime_started_at',
  'downtime_ended_at',
  'approvals',
  'cutover_requested',
  'cutover_requested_by',
  'force_cutover',
  'force_window',
  'advisor_hold',
  'review_required',
  'review_reason',
  'error',
  'source_running_at',
  'findings',
  'suggested_probes',
  'cutover_refusals',
  'gpu_wait',
  'created_at',
  'updated_at',
] as const;

/**
 * `GET /migrations?view=summary` (SDD §4, §12): the fields of a migration the Overview's and the plan page's lists
 * read - no passes, all the estimates, notes, history or VM details. `phase_since` is the last phase change's time,
 * when the phase it is in began, else the creation time; `preflight_ran` whether the history reaches ready or
 * blocked; `eligible_strategies` the estimates' eligible strategies, in their order.
 */
export interface MigrationSummary
  extends Pick<Migration, Exclude<(typeof MIGRATION_SUMMARY_FIELDS)[number], 'vm' | 'phase_since' | 'preflight_ran' | 'eligible_strategies'>> {
  vm: Pick<VMRef, 'source_id' | 'name' | 'project'>;
  phase_since: Timestamp;
  preflight_ran: boolean;
  eligible_strategies: Strategy[];
}

/** A migration or its summary: what a list page reads of either (SDD §4, §16). */
export type MigrationLike = Migration | MigrationSummary;

// ---------------------------------------------------------------------------------------------
// §4.3 Event kinds
// ---------------------------------------------------------------------------------------------

export const PERSISTED_EVENT_KINDS = [
  'plan.created',
  'plan.updated',
  'plan.validated',
  'plan.started',
  'plan.paused',
  'plan.completed',
  'wave.started',
  'wave.completed',
  'migration.created',
  'migration.phase',
  'migration.sync_pass',
  'migration.downtime_started',
  'migration.downtime_ended',
  'migration.error',
  'migration.force_cutover',
  'migration.data_inconsistent',
  'migration.source_running',
  'migration.source_stopped',
  // once when a cutover begins to wait for GPU capacity, once when it ends (SDD §4.3, §22.7)
  'migration.gpu_wait',
  'plan.finalized',
  'plan.cancelled',
  'migration.approved',
  'migration.action',
  'advisor.strategy',
  'advisor.classification',
  'advisor.verification',
  'advisor.similar_incidents',
  'memory.lesson_saved',
  'provider.created',
  'provider.updated',
  'provider.credentials_updated',
  'provider.deleted',
  'provider.checked',
  // the hook catalog's changes (SDD §4.3, §23.3, plan task N3)
  'hook.created',
  'hook.updated',
  'hook.deleted',
  'auth.denied',
  'user.created',
  'events.pruned',
  // audit events (SDD §14.5)
  'recommendation.created',
  'decision.auto_applied',
  'decision.pending_approval',
  'decision.approved',
  'decision.overridden',
  'decision.below_threshold',
  'decision.fallback',
  'policy.created',
  'policy.updated',
  'policy.deleted',
] as const;
export type PersistedEventKind = (typeof PERSISTED_EVENT_KINDS)[number];

/** The audit events: who decided what, when, with which model version (SDD §14.5). */
export const AUDIT_EVENT_KINDS = [
  'recommendation.created',
  'decision.auto_applied',
  'decision.pending_approval',
  'decision.approved',
  'decision.overridden',
  'decision.below_threshold',
  'decision.fallback',
  'policy.created',
  'policy.updated',
  'policy.deleted',
  // a cutover before convergence, with its approver and reason (SDD §5.4, §14.5, ADV-16)
  'migration.force_cutover',
] as const;
export type AuditEventKind = (typeof AUDIT_EVENT_KINDS)[number];

/** Bus/SSE only, never stored; delivered with SSE `id: 0`. */
export const EPHEMERAL_EVENT_KINDS = ['migration.progress', 'migration.log', 'heartbeat'] as const;
export type EphemeralEventKind = (typeof EPHEMERAL_EVENT_KINDS)[number];

export type EventKind = PersistedEventKind | EphemeralEventKind;

export interface Event {
  seq: number;
  ts: Timestamp;
  /** One of {@link EventKind}; typed as string because newer servers may add kinds. */
  kind: EventKind | (string & {});
  plan_id: string | null;
  migration_id: string | null;
  actor: string;
  message: string;
  data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------------------------
// §10 Inventory shapes returned by GET /providers/{id}/inventory
// ---------------------------------------------------------------------------------------------

export interface DestinationFlavor {
  name: string;
  vcpus: number;
  ram_mb: number;
  disk_gb: number;
  extra_specs: Record<string, string>;
}

/** Free amounts per destination project (SDD §10): -1 is unlimited, null not reported. */
export interface ProjectQuota {
  cores: number | null;
  ram_mb: number | null;
  instances: number | null;
  volumes: number | null;
  gigabytes: number | null;
}

export interface DestinationInventory {
  /** name -> MTU */
  networks: Record<string, number | null>;
  flavors: DestinationFlavor[];
  volume_types: string[];
  /** project -> free amounts */
  quotas: Record<string, ProjectQuota>;
  projects: string[];
  /** The subnets the destination credential sees: where a NIC's addresses must lie (SDD §10). */
  subnets?: DestinationSubnet[];
  /** Every network the destination credential sees, with its owner: what a NIC's network name resolves to (SDD §10). */
  network_details?: DestinationNetwork[];
  /** The role names of the destination credential's token, read live with the inventory; null when unreadable (SDD §10, RV19-01). */
  roles?: string[] | null;
  /** The flavors whose extra specs ask for a GPU, with the profile their provider declares (SDD §22.3); older answers omit it. */
  gpu_flavors?: GpuFlavor[];
  /** The Placement pools of their vGPU requests, read with the inventory (SDD §22.3). */
  gpu_capacity?: GpuCapacity;
  /** The addresses of the credential's project's floating IPs that no port holds: what os-migrate's `auto` attaches (SDD §10). */
  floating_ips?: string[];
  /** Neutron's DNS integration and Designate's zones (SDD §10, §9.3). */
  dns?: DnsRead;
}

/** Whether Neutron lists the dns-integration extension, and the Designate zones the credential sees; null where they could
 * not be read, which is not judged (SDD §10, plan task N2b). */
export interface DnsRead {
  extension: boolean | null;
  zones: string[] | null;
}

/** A destination flavor that asks for a GPU, with its GPU requests (SDD §22.3). */
export interface GpuFlavor {
  name: string;
  vcpus: number;
  ram_mb: number;
  disk_gb: number;
  gpus: GpuDevice[];
}

export interface DestinationSubnet {
  /** What a VMware NIC's network_map value names (SDD §10, §7.2); an inventory from before it lacks it. */
  id?: string;
  name: string;
  network: string;
  /** The network the subnet belongs to (an inventory from before it lacks it). */
  network_id?: string;
  cidr: string;
  ipv6_address_mode?: string | null;
  ipv6_ra_mode?: string | null;
}

export interface DestinationNetwork {
  id: string | null;
  name: string;
  mtu: number | null;
  project_id: string | null;
  /** The destination credential's project owns it. */
  own: boolean;
  /** Neutron's `shared` and `router:external` as the credential sees them: whether it may put a port on another project's network (SDD §6.6). */
  shared: boolean;
  external?: boolean;
}

/** `VMRef[]` for a source provider, `DestinationInventory` for a destination provider. */
export type ProviderInventory = VMRef[] | DestinationInventory;

// ---------------------------------------------------------------------------------------------
// §12 REST API request / response shapes
// ---------------------------------------------------------------------------------------------

export interface ApiErrorEnvelope {
  error: { code: string; message: string };
}

export interface OrchestratorHealth {
  running: boolean;
  /** Seconds since the last completed tick; null before the first one. */
  last_tick_age_s: number | null;
  ticks: number;
  healthy: boolean;
}

export interface Health {
  status: 'ok' | 'degraded';
  version: string;
  demo: boolean;
  /** Demo mode's time compression (SDD §7.4): a wall second is this many model seconds; null outside demo mode. */
  demo_speed: number | null;
  /** Authentication is off (SDD §13.1): every request is the anonymous admin, so no token is needed. */
  auth_disabled: boolean;
  db: 'ok' | 'error';
  orchestrator: OrchestratorHealth;
}

export interface Me {
  name: string;
  role: Role;
}

export interface UserCreated extends Me {
  token: string;
}


type PlanServerFields = 'id' | 'waves' | 'status' | 'derived_status' | 'migration_counts' | 'created_at' | 'updated_at';
type PlanRequired = 'name' | 'source_provider_id' | 'destination_provider_id' | 'vm_ids';

/** Plan fields minus id/waves/status/created_at/updated_at; name, providers and vm_ids required. */
export type PlanCreate = Pick<Plan, PlanRequired> & Partial<Omit<Plan, PlanServerFields | PlanRequired>>;

/** PATCH /plans/{id}: partial PlanCreate (only in draft/validated; resets status to draft). */
export type PlanPatch = Partial<PlanCreate>;

export interface AutoWavesRequest {
  max_wave_size?: number;
}

export interface ValidationReportMigration {
  migration_id: string;
  vm_name: string;
  strategy: Strategy;
  phase: Phase;
  findings: Finding[];
  estimates: Estimate[];
}

export interface ValidationReport {
  plan_id: string;
  ok: boolean;
  migrations: ValidationReportMigration[];
}

export interface MigrationListQuery {
  plan_id?: string;
  phase?: Phase;
  wave_id?: string;
  /** Page large plans: 1…5000 (default: every migration), creation order. */
  limit?: number;
  offset?: number;
  /** `summary`: each migration as its {@link MigrationSummary} (SDD §12); not a filter. */
  view?: 'full' | 'summary';
}

/** The latest passes `sync_passes` keeps after the first `plan.max_sync_passes` (SDD §5.4). */
export const SYNC_PASSES_LATEST = 20;
/** Longest `comment`, `reason` or `confirm` a migration action accepts (SDD §12). */
export const ACTION_TEXT_MAX = 2000;
/** Its field's maxLength in the UTF-16 units the browser counts: room for ACTION_TEXT_MAX characters of any kind, an
 * emoji two units; the dialog refuses more than ACTION_TEXT_MAX as the API counts them (review RV50-06). */
export const ACTION_TEXT_UNITS = 2 * ACTION_TEXT_MAX;
/** Longest plan name and description POST/PATCH /plans accept (SDD §12). */
export const PLAN_NAME_MAX = 200;
export const PLAN_DESCRIPTION_MAX = 2000;
/** Bounds POST /advisor/similar-incidents accepts: query length and result count (SDD §12). */
export const ADVISOR_QUERY_MAX = 2000;
export const ADVISOR_LIMIT_MAX = 20;

export interface ApproveRequest {
  comment?: string;
}

export interface CutoverRequest {
  force_window?: boolean;
  comment?: string;
}

/** POST /migrations/{id}/force-cutover (SDD §5.4, §12). */
export interface ForceCutoverRequest {
  reason: string;
}

/** The shortest reason a Force cutover accepts (SDD §12). */
export const FORCE_REASON_MIN = 10;

export interface RollbackRequest {
  reason: string;
}

export interface CancelRequest {
  reason?: string;
}

export interface FinalizeRequest {
  delete_source?: boolean;
  /** Must equal `vm.name`. */
  confirm: string;
}

/** POST /plans/{id}/finalize (SDD §8.1): a wave, or the plan, with one approval. */
export interface PlanFinalizeRequest {
  wave_id: string | null;
  /** The wave's name, or the plan's without `wave_id`. */
  confirm: string;
  delete_source: boolean;
}

export interface PlanFinalizeResult {
  finalized: string[];
  skipped: Array<{ migration_id: string; reason: string }>;
}

export interface StrategyRequest {
  strategy: Strategy;
}

export interface EventListQuery {
  since?: number;
  /** Only events below this seq: with `tail`, the page before the oldest one shown (SDD §12). */
  before?: number;
  plan_id?: string;
  migration_id?: string;
  /** ≤ 1000 */
  limit?: number;
  /** The newest `limit` matching events instead of the first, still ascending (SDD §12). */
  tail?: boolean;
  /** Only the audit events (SDD §14.5). */
  audit?: boolean;
}

export interface ThroughputPoint {
  ts: Timestamp;
  /** Bytes per second (the control plane's `_bps` fields are bytes/s, e.g. link_bps = 125 MiB/s). */
  bps: number;
}

export interface Stats {
  total: number;
  by_phase: Partial<Record<Phase, number>>;
  completed: number;
  failed: number;
  in_progress: number;
  bytes_transferred: number;
  avg_downtime_s: number | null;
  p95_downtime_s: number | null;
  max_downtime_s: number | null;
  slo_compliance_pct: number | null;
  /** Completed migrations whose numbers do not hold, left out of the downtime figures (SDD §5.5). */
  data_inconsistent: number;
  /** `completed` = these two: verified and waiting, and finalized (SDD §8.1, ADV-19). */
  awaiting_finalize: number;
  finalized: number;
  downtime_by_strategy: Partial<Record<Strategy, number>>;
  /** Last 60 one-minute buckets. */
  throughput_series: ThroughputPoint[];
}

/** Where the Jev model runs (SDD §13.4, ADV-06). */
export type JevDeployment = 'on_prem' | 'remote';

export interface AdvisorStatus {
  jev: {
    mode: string;
    available: boolean;
    last_error?: string | null;
    deployment: JevDeployment;
    endpoint_host: string | null;
    /** Whether the console excerpt may leave the installation for screening. */
    console_sent: boolean;
  };
  memory: { enabled: boolean; available: boolean; last_error?: string | null };
}

/** GET /advisor/data-sent: what each decision type sends to Jev, from the serializers' field specs (SDD §13.4). */
export interface DataSentField {
  key: string;
  label: string;
}

export interface DataSentItem {
  decision_type: DecisionType;
  tool: string;
  fields: DataSentField[];
  sent: boolean;
  note: string | null;
}

export interface DataSentList {
  items: DataSentItem[];
}

export interface SimilarIncidentsRequest {
  query: string;
  limit?: number;
}

export interface SimilarIncidentHit {
  title: string;
  content: string;
  score?: number | null;
}

export interface SimilarIncidentsResponse {
  hits: SimilarIncidentHit[];
}

/** Query of `GET /advisor/recommendations` (SDD §12). */
export interface RecommendationListQuery {
  migration_id?: string;
  plan_id?: string;
  decision_type?: DecisionType;
  limit?: number;
  offset?: number;
}

/** Bounds and default of the `limit` `GET /advisor/recommendations` accepts (SDD §12). */
export const RECOMMENDATION_LIMIT_MAX = 200;
export const RECOMMENDATION_LIMIT_DEFAULT = 50;

/** `GET /advisor/recommendations`: newest first, stored and legacy entries together (SDD §12, §14.4). */
export interface RecommendationPage {
  items: AdvisorRecommendation[];
  total: number;
  offset: number;
  limit: number;
}

/** Bounds and default of the `limit` `GET /advisor/notes` accepts (SDD §12). */
export const ADVISOR_NOTES_LIMIT_MAX = 200;
export const ADVISOR_NOTES_LIMIT_DEFAULT = 20;

/** An advisor note with the migration it belongs to (SDD §12 `GET /advisor/notes`). */
export interface AdvisorNoteEntry extends AdvisorNote {
  migration_id: string;
  plan_id: string;
  vm_name: string;
}

/** `GET /advisor/notes`: every migration's notes but strategy notes, newest first (SDD §12). */
export interface AdvisorNoteList {
  items: AdvisorNoteEntry[];
}

/** Migration actions exposed as `POST /migrations/{id}/<action>` (SDD §12). */
export const MIGRATION_ACTIONS = ['approve', 'cutover', 'force-cutover', 'sync', 'rollback', 'retry', 'cancel', 'finalize'] as const;
export type MigrationAction = (typeof MIGRATION_ACTIONS)[number];

export function isDestinationInventory(inventory: ProviderInventory): inventory is DestinationInventory {
  return !Array.isArray(inventory);
}
