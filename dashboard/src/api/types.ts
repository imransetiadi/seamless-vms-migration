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

export const PLAN_STATUSES = ['draft', 'validated', 'running', 'paused', 'completed', 'failed'] as const;
export type PlanStatus = (typeof PLAN_STATUSES)[number];

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
  /** Server-owned: when the write-only credentials / conversion key were last stored. */
  credentials_updated_at: Timestamp | null;
  conversion_key_updated_at: Timestamp | null;
}

/** `POST /providers` body (status and server-owned fields are ignored by the API). */
export type ProviderCreate = Pick<Provider, 'id' | 'name' | 'kind' | 'role' | 'endpoint' | 'cloud' | 'region' | 'verify_tls' | 'ca_cert_path' | 'conversion_host' | 'distribution' | 'credentials_secret'>;

/** `PATCH /providers/{id}`: the editable fields (SDD §12). */
export type ProviderPatch = Partial<Pick<Provider, 'name' | 'endpoint' | 'cloud' | 'credentials_secret' | 'region' | 'verify_tls' | 'ca_cert_path' | 'conversion_host' | 'distribution'>>;

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
  /** Derived and serialized: Σ size_gb · 2^30. */
  disk_bytes: number;
  /** Derived and serialized: Σ used_gb · 2^30 when every disk has used_gb, else ⌊disk_bytes · 0.6⌋. */
  used_bytes: number;
  /** Derived and serialized from `os_type` (SDD §9.5); absent in responses of older control planes. */
  guest_os?: GuestOS;
}

export interface Mappings {
  networks: Record<string, string>;
  flavors: Record<string, string>;
  volume_types: Record<string, string>;
  projects: Record<string, string>;
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

export interface VerificationConfig {
  tcp_ports: number[];
  /** Probed instead of `tcp_ports` for Windows guests, whose console check is skipped (SDD §7.5). */
  windows_tcp_ports: number[];
  probe_address: 'fixed' | 'floating';
  console_success_patterns: string[];
  timeout_s: number;
  auto_rollback: boolean;
  use_advisor: boolean;
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
  prestage_resources: string[];
  waves: Wave[];
  status: PlanStatus;
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
}

export interface AdvisorNote {
  kind: AdvisorNoteKind;
  source: AdvisorNoteSource;
  summary: string;
  confidence: number | null;
  data: Record<string, unknown>;
  created_at: Timestamp;
}

export interface Approval {
  actor: string;
  at: Timestamp;
  comment: string | null;
}

export interface PhaseChange {
  from_phase: Phase | null;
  to_phase: Phase;
  at: Timestamp;
  reason: string;
  actor: string;
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
  force_window: boolean;
  advisor_notes: AdvisorNote[];
  review_required: boolean;
  review_reason: string | null;
  destination_server_id: string | null;
  error: string | null;
  attempts: number;
  created_at: Timestamp;
  updated_at: Timestamp;
}

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
  'auth.denied',
] as const;
export type PersistedEventKind = (typeof PERSISTED_EVENT_KINDS)[number];

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

export interface ProjectQuota {
  cores: number;
  ram_mb: number;
  instances: number;
  volumes: number;
  gigabytes: number;
}

export interface DestinationInventory {
  /** name -> MTU */
  networks: Record<string, number | null>;
  flavors: DestinationFlavor[];
  volume_types: string[];
  /** project -> free amounts */
  quotas: Record<string, ProjectQuota>;
  projects: string[];
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
  db: 'ok' | 'error';
  orchestrator: OrchestratorHealth;
}

export interface Me {
  name: string;
  role: Role;
}


type PlanServerFields = 'id' | 'waves' | 'status' | 'created_at' | 'updated_at';
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
}

export interface ApproveRequest {
  comment?: string;
}

export interface CutoverRequest {
  force_window?: boolean;
  comment?: string;
}

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

export interface StrategyRequest {
  strategy: Strategy;
}

export interface EventListQuery {
  since?: number;
  plan_id?: string;
  migration_id?: string;
  /** ≤ 1000 */
  limit?: number;
  /** The newest `limit` matching events instead of the first, still ascending (SDD §12). */
  tail?: boolean;
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
  downtime_by_strategy: Partial<Record<Strategy, number>>;
  /** Last 60 one-minute buckets. */
  throughput_series: ThroughputPoint[];
}

export interface AdvisorStatus {
  jev: { mode: string; available: boolean; last_error?: string | null };
  memory: { enabled: boolean; available: boolean; last_error?: string | null };
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

/** Migration actions exposed as `POST /migrations/{id}/<action>` (SDD §12). */
export const MIGRATION_ACTIONS = ['approve', 'cutover', 'sync', 'rollback', 'retry', 'cancel', 'finalize'] as const;
export type MigrationAction = (typeof MIGRATION_ACTIONS)[number];

export function isDestinationInventory(inventory: ProviderInventory): inventory is DestinationInventory {
  return !Array.isArray(inventory);
}
