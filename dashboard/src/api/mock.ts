/**
 * In-browser mock adapter (VITE_SEAMLESS_MOCK=1, SDD §16).
 *
 * `createMockFetch(server)` returns a `fetch` replacement that serves the SDD §12 routes from an
 * in-memory `MockServer`, including `GET /events/stream` as a real SSE byte stream, so the API
 * client, SSE parser and every page run through exactly the same code paths as against the
 * control plane. Roles are enforced per route; phase transitions follow SDD §5.1.
 *
 * Mock tokens: `viewer`, `operator`, `approver` or `admin` select a role; no token means the
 * anonymous admin of demo mode on loopback (SDD §13.1); `locked` stands for the per-address lockout
 * (429); any other token is rejected with 401. Every refusal is audited as `auth.denied` like the API.
 */
import { DEFAULT_API_BASE } from './client';
import { ESTIMATOR_DEFAULTS, forcedDowntime } from '../lib/forcedEstimate';
import { canTransition } from '../lib/fsm';
import { guestOsOf } from '../lib/guestOs';
import { capabilityRoles, policyProfileOf } from '../lib/networks';
import { canonicalIp, isIpAddress } from '../lib/nics';
import { blocksPlanChange, inFlightRefusal } from '../lib/inFlight';
import { countedApprovals, sourceStopped } from '../lib/migrationActions';
import { resolvePolicy } from '../lib/policies';
import { probesFor } from '../lib/probes';
import { hasRole, OWN_CUTOVER, OWN_DECISION, OWN_FORCE } from '../lib/roles';
import { stable } from '../lib/stable';
import { endedPassBytes } from '../lib/transfer';
import { summaryOf } from '../lib/summary';
import { isWaveActive, WAVE_COMPLETE_PHASES } from '../lib/waves';
import { destinationName, isBlank, providerLabel } from '../lib/providerUse';
import { pyFloat, pyLen, pyRepr, pyStrip } from '../lib/format';
import {
  buildFixtures,
  defaultPlanFields,
  eligibility,
  estimate,
  GiB,
  iso,
  JEV_MIN_CONFIDENCE,
  MiB,
  preflight,
  prng,
  auditEventsOf,
  decisionEventOf,
  humanDecision,
  strategyRecommendation,
  strategySnapshot,
  destinationDescriptor,
  DEMO_TOKEN_ROLES,
  type StrategyDecision,
  checkOf,
  vmwareCheck,
  HELD_VOLUMES_REASON,
} from './mockData';
import { buildStrategyEvidence, evidenceCount } from './mockEvidence';
import { derivePlanStatus } from './mockPlanStatus';
import { isDatabase, suggestProbes } from './mockProbes';
import type {
  AppProbe,
  Health,
  ProbeKind,
  ProbeResult,
  VerificationCheck,
  AdvisorNoteEntry,
  AdvisorNoteList,
  AdvisorPolicy,
  AdvisorPolicySpec,
  AdvisorRecommendation,
  ReasonCode,
  AdvisorSnapshot,
  AdvisorStatus,
  ConvergedBy,
  DataSentItem,
  DecisionType,
  DestinationInventory,
  Estimate,
  Event,
  Me,
  Migration,
  Phase,
  Plan,
  PlanCreate,
  Provider,
  RecommendationPage,
  Role,
  Stats,
  Strategy,
  SyncPass,
  ValidationReport,
  VMRef,
  Wave,
} from './types';
import {
  ACTION_TEXT_MAX,
  ADVISOR_LIMIT_MAX,
  ADVISOR_NOTES_LIMIT_DEFAULT,
  ADVISOR_NOTES_LIMIT_MAX,
  ADVISOR_QUERY_MAX,
  AUDIT_EVENT_KINDS,
  DECISION_TYPES,
  FORCE_REASON_MIN,
  MAX_PROBE_PATH,
  MAX_PROBES_PER_VM,
  OVERRIDE_REASON_MIN,
  PHASES,
  POLICY_MODES,
  PLAN_DESCRIPTION_MAX,
  PLAN_NAME_MAX,
  PROBE_KINDS,
  RECOMMENDATION_LIMIT_DEFAULT,
  RECOMMENDATION_LIMIT_MAX,
  ROLES,
  STRATEGIES,
  SYNC_PASSES_LATEST,
} from './types';

interface MockResponse {
  status: number;
  body?: unknown;
  text?: string;
}

/** A record the store lacks, worded as the API's NotFound: "provider 'ghost' not found" (review RV24-06). */
function notFound(kind: string, id: string): HttpError {
  return new HttpError(404, 'not_found', `${kind} ${pyRepr(id)} not found`);
}

/** The fields of PUT /providers/{id}/credentials, as the API's ProviderCredentialsRequest: in its order, each with its longest value. */
const CREDENTIAL_FIELDS: ReadonlyArray<readonly [string, number | null]> = [
  ['auth_url', 2048],
  ['username', 256],
  ['password', 4096],
  ['project_name', 256],
  ['user_domain_name', 256],
  ['project_domain_name', 256],
  ['application_credential_id', 256],
  ['application_credential_secret', 4096],
  ['interface', null],
  ['datacenter', 256],
];

/**
 * What a PUT of a provider's credentials or conversion key refuses from its body alone, before it looks the provider up, as the
 * API's request models (extra fields forbidden): worded as its validation_message over pydantic's errors - the model's fields
 * in their order, then the extra inputs, five at most; a length counts code points (SDD §12, §13.3, review RV23-03). Bodies
 * no client sends stay as they are, the mock reading a parsed body: none or a JSON null, a non-object, a lone surrogate, and
 * integer-like extra keys, which JavaScript lists first (review RV24-06).
 */
function refuseCredentialBody(route: 'credentials' | 'conversion-key', input: Record<string, unknown>): void {
  const fields = route === 'credentials' ? CREDENTIAL_FIELDS : ([['private_key', 16384]] as const);
  const problems: string[] = [];
  for (const [key, longest] of fields) {
    const value = input[key];
    if (!Object.hasOwn(input, key)) {
      // every credential is optional; the conversion key is not
      if (route === 'conversion-key') problems.push(`${key}: Field required`);
      continue;
    }
    if (value === null && route === 'credentials') continue;
    if (key === 'interface') {
      if (!['public', 'internal', 'admin'].includes(value as string)) problems.push("interface: Input should be 'public', 'internal' or 'admin'");
      continue;
    }
    if (typeof value !== 'string') {
      problems.push(`${key}: Input should be a valid string`);
      continue;
    }
    const length = [...value].length;
    if (route === 'conversion-key' && length < 64) problems.push(`${key}: String should have at least 64 characters`);
    else if (longest !== null && length > longest) problems.push(`${key}: String should have at most ${longest} characters`);
  }
  const known = new Set(fields.map(([key]) => key));
  for (const key of Object.keys(input)) if (!known.has(key)) problems.push(`${key}: Extra inputs are not permitted`);
  if (problems.length) throw new HttpError(422, 'validation_error', problems.slice(0, 5).join('; '));
}

/**
 * An override body as the API's request model OverrideRequest reads it (SDD §12, §14.6, reviews RV42B-01, RV43B-07):
 * `option` a strategy name, `reason` a string of at most 2000 characters or null, nothing else - refused before the
 * recommendation is looked up, worded as its validation_message over pydantic's errors: the model's fields in their
 * order, then the extra inputs, five at most.
 */
function refuseOverrideBody(input: Record<string, unknown>): void {
  const quoted = STRATEGIES.map((s) => `'${s}'`);
  const problems: string[] = [];
  if (!Object.hasOwn(input, 'option')) problems.push('option: Field required');
  else if (!STRATEGIES.includes(input.option as Strategy)) {
    problems.push(`option: Input should be ${quoted.slice(0, -1).join(', ')} or ${quoted.at(-1)}`);
  }
  if (Object.hasOwn(input, 'reason') && input.reason !== null) {
    if (typeof input.reason !== 'string') problems.push('reason: Input should be a valid string');
    else if ([...input.reason].length > 2000) problems.push('reason: String should have at most 2000 characters');
  }
  for (const key of Object.keys(input)) if (key !== 'option' && key !== 'reason') problems.push(`${key}: Extra inputs are not permitted`);
  if (problems.length) throw new HttpError(422, 'validation_error', problems.slice(0, 5).join('; '));
}

/**
 * A plan cancel's body as the API's CancelRequest reads it (SDD §12, PR #58 review): `reason` a string of at most
 * ACTION_TEXT_MAX characters or null, nothing else - worded as its validation_message over pydantic's errors.
 */
function refuseCancelBody(input: Record<string, unknown>): void {
  const problems: string[] = [];
  if (Object.hasOwn(input, 'reason') && input.reason !== null) {
    if (typeof input.reason !== 'string') problems.push('reason: Input should be a valid string');
    else if ([...input.reason].length > ACTION_TEXT_MAX) problems.push(`reason: String should have at most ${ACTION_TEXT_MAX} characters`);
  }
  for (const key of Object.keys(input)) if (key !== 'reason') problems.push(`${key}: Extra inputs are not permitted`);
  if (problems.length) throw new HttpError(422, 'validation_error', problems.slice(0, 5).join('; '));
}

/** A route whose request is `{}`, like the API's EmptyRequest (SDD §12, audit AA-04): an empty object or none, a field refused. */
function refuseEmptyBody(input: Record<string, unknown>): void {
  const problems = Object.keys(input).map((key) => `${key}: Extra inputs are not permitted`);
  if (problems.length) throw new HttpError(422, 'validation_error', problems.slice(0, 5).join('; '));
}

/** The secrets a provider reads, by name, for its audit events - never a value: the API's _secret_names (SDD §13.3). */
function secretNames(provider: Provider): Record<string, string> {
  return {
    ...(provider.credentials_secret ? { credentials_secret: provider.credentials_secret } : {}),
    ...(provider.conversion_host?.ssh_key_secret ? { ssh_key_secret: provider.conversion_host.ssh_key_secret } : {}),
  };
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

const PERSONAS: Record<Role, string> = { viewer: 'dimas', operator: 'bayu', approver: 'sari', admin: 'rina' };
const SINGLE_SHOT: ReadonlySet<Strategy> = new Set(['cold', 'storage_handover', 'vmware_cold']);
/** orchestrator.APPROVABLE and CUTOVER_REQUESTABLE (SDD §5.4). */
const APPROVABLE: ReadonlySet<Phase> = new Set(['pending', 'validating', 'blocked', 'ready', 'precopy', 'syncing', 'awaiting_cutover', 'failed', 'rolled_back']);
const CUTOVER_REQUESTABLE: ReadonlySet<Phase> = new Set(['ready', 'precopy', 'syncing', 'awaiting_cutover']);
/** A migration in these stopped its source (its cutover did, SDD §8.1). */
const STOPPED_SOURCE: ReadonlySet<Phase> = new Set(['cutover', 'verifying', 'completed', 'finalized']);
/** orchestrator.FORCEABLE (SDD §5.4, ADV-16). */
const FORCEABLE: ReadonlySet<Phase> = new Set(['precopy', 'syncing']);
const SIMPLICITY: Record<Strategy, number> = { cold: 0, storage_handover: 1, warm: 2, vmware_cold: 0, vmware_warm: 1 };
const MAX_EVENTS = 5000;

function ok(body: unknown, status = 200): MockResponse {
  return { status, body };
}

function sseFrame(event: Event): string {
  return `id: ${event.seq}\nevent: ${event.kind}\ndata: ${JSON.stringify(event)}\n\n`;
}

function hex(rand: () => number, length: number): string {
  let out = '';
  for (let i = 0; i < length; i += 1) out += Math.floor(rand() * 16).toString(16);
  return out;
}

function percentile(sorted: number[], p: number): number | null {
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[Math.max(0, index)] ?? null;
}

export interface MockServerOptions {
  now?: () => number;
  seed?: number;
  /**
   * The advisor's answer on an automatic choice between the strategies of `scope` (jev_decide in the
   * API, SDD §14.2); a null option and confidence is no answer. Defaults to `simulatedAdvice`.
   */
  advise?: (vm: VMRef, scope: readonly Strategy[], estimates: readonly Estimate[]) => SimulatedAdvice;
}

export interface SimulatedAdvice {
  option: string | null;
  confidence: number | null;
}

/**
 * Demo mode's stand-in for Jev: the scope's shortest downtime at a confidence of 0.50-0.94 that
 * depends on the VM alone, so it is the same every time and never draws from the seeded stream.
 */
export function simulatedAdvice(vm: VMRef, scope: readonly Strategy[], estimates: readonly Estimate[]): SimulatedAdvice {
  let hash = 2166136261; // FNV-1a over the VM id
  for (const char of vm.source_id) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  const downtime = (s: Strategy) => estimates.find((e) => e.strategy === s)?.downtime_s ?? Infinity;
  const option = [...scope].sort((a, b) => downtime(a) - downtime(b))[0] ?? null;
  return { option, confidence: 0.5 + (hash % 45) / 100 };
}

/** selector.advisor_scope (SDD §9.2, §14.2): the strategies the advisor may choose between; empty = no advice. */
function advisorScope(estimates: readonly Estimate[], policy: Plan['selection_policy']): Strategy[] {
  const eligible = estimates.filter((e) => e.eligible);
  if (!eligible.length) return [];
  const bySimplicity = (list: Estimate[]) => list.map((e) => e.strategy).sort((a, b) => SIMPLICITY[a] - SIMPLICITY[b]);
  if (!eligible.some((e) => e.meets_slo)) return bySimplicity(eligible);
  if (policy === 'simplest_meeting_slo') return [];
  const best = Math.min(...eligible.map((e) => e.downtime_s));
  const ties = eligible.filter((e) => e.downtime_s - best < Math.max(60, best * 0.1));
  return ties.length >= 2 ? bySimplicity(ties) : [];
}

/**
 * What the API's serializers send (GET /advisor/data-sent, SDD §13.4): the server generates it from its field
 * specs; the mock repeats it for a remote model whose console stays local - word for word, as mock.test.ts holds it to
 * the API's own list (seamless/tests/fixtures/data_sent.json, review RV21-03).
 */
const LOCAL_CONSOLE = 'The console excerpt stays in this installation: the advisor is remote and SEAMLESS_CONSOLE_SCREENING_SEND_TO_REMOTE is off.';
const DATA_SENT: DataSentItem[] = [
  {
    decision_type: 'migration_strategy',
    tool: 'jev_decide',
    fields: [
      { key: 'decision', label: "The question, naming the VM and the destination's kind and series (never a provider's name)" },
      { key: 'vm', label: 'VM name, vCPU, RAM and number of disks' },
      { key: 'disks', label: 'Each disk: kind, size, volume type, multi-attach, encrypted, independent' },
      { key: 'size', label: 'Provisioned and used size' },
      { key: 'write_rate', label: 'Guest write rate, measured or assumed' },
      { key: 'slo', label: "The plan's downtime SLO" },
      { key: 'link', label: "The plan's link bandwidth" },
      { key: 'findings', label: 'Pre-flight finding codes and severities' },
      { key: 'estimates', label: 'Per strategy: downtime, pre-copy, passes and eligibility with its reasons' },
      { key: 'priorities', label: "Minimize downtime within the plan's SLO; the simpler strategy on near ties" },
      { key: 'candidates', label: 'The eligible strategies with their descriptions' },
      { key: 'requirements', label: 'The two requirements every strategy must meet' },
    ],
    sent: true,
    note: null,
  },
  {
    decision_type: 'workload_tier',
    tool: 'jev_classify',
    fields: [
      { key: 'id', label: "The item's place in the batch (item-1, item-2 …), not the VM's id" },
      { key: 'name', label: 'VM name' },
      { key: 'os', label: 'Guest OS' },
      { key: 'tags', label: 'Tags' },
      { key: 'disks', label: 'Disk sizes' },
      { key: 'flavor', label: 'Flavor, or vCPU and RAM' },
    ],
    sent: true,
    note: null,
  },
  {
    decision_type: 'verification_review',
    tool: 'jev_verify',
    fields: [
      { key: 'claims', label: 'The two boot claims to verify' },
      { key: 'checks', label: 'Each verification check: name, result and detail' },
    ],
    sent: true,
    note: LOCAL_CONSOLE,
  },
  { decision_type: 'console_screening', tool: 'jev_screen', fields: [{ key: 'console', label: 'The last 6,000 characters of the console, redacted' }], sent: false, note: LOCAL_CONSOLE },
];

/**
 * A vmware_warm `m`'s passes since the kit last released its copies (the API's `_passes_since_the_kit_released_its_copies`,
 * SDD §9.3, §7.2, reviews RV40-05, RV41B-02): those that started once the attempt after its last cutover entered pre-copy -
 * a cutover's own final pass not among them -, every pass when no cutover ran, none when its last attempt ran one.
 */
function passesSinceTheKitReleasedItsCopies(m: Migration): Migration['sync_passes'] {
  let entered: string | undefined; // when the first attempt after the last cutover entered pre-copy
  for (const change of [...m.phase_history].reverse()) {
    if (change.to_phase === 'cutover') {
      if (entered === undefined) return [];
      const since = Date.parse(entered);
      return m.sync_passes.filter((p) => Date.parse(p.started_at) >= since);
    }
    if (change.to_phase === 'precopy') entered = change.at;
  }
  return m.sync_passes;
}

/** `est` ineligible with §9.2's reason when it is an eligible storage handover (the API's `_held_out`, SDD §12). */
function heldOut(est: Estimate): Estimate {
  if (est.strategy !== 'storage_handover' || !est.eligible) return est;
  return { ...est, eligible: false, reasons: [...est.reasons, HELD_VOLUMES_REASON] };
}

/**
 * `est` without §9.2's held reason once nothing is held: eligible again when that reason was its only one, its other
 * reasons kept (the API's `_held_in`, SDD §12, review RV43B-02).
 */
function heldIn(est: Estimate): Estimate {
  if (est.strategy !== 'storage_handover' || !est.reasons.includes(HELD_VOLUMES_REASON)) return est;
  const reasons = est.reasons.filter((reason) => reason !== HELD_VOLUMES_REASON);
  return { ...est, eligible: reasons.length === 0, reasons };
}

/**
 * The strategy of the attempt whose copies remain (the API's `_copies_strategy`, SDD §9.3, review RV40-06): while `m`
 * waits for its next attempt, the one a Retry recorded - a validation or a strategy change may have chosen another
 * since -, else its own.
 */
function copiesStrategy(m: Migration): Strategy {
  const waiting = ['pending', 'validating', 'blocked', 'ready'].includes(m.phase);
  return waiting && m.attempt_strategies.length ? m.attempt_strategies[m.attempt_strategies.length - 1]! : m.strategy;
}

/**
 * What a rollback kept of `m`'s copies (SDD §5.1, §9.3, reviews RV39-02, RV40-05, RV41B-02): the mock keeps no workload
 * record, so as the API without one (`_kept_after_rollback`) - a warm migration's passes, and a vmware_warm one's passes
 * since the kit last released its copies (its rollback deletes them after a cutover, or marks them cutover; an attempt
 * with no cutover leaves them marked for the next run's CBT sync); a cold, vmware_cold or handover rollback keeps no copy.
 * It is the destination volumes a migration holds (§9.3) and the copied data it keeps (§5.1).
 */
function keptAfterRollback(m: Migration): boolean {
  const strategy = copiesStrategy(m);
  if (strategy === 'warm') return m.sync_passes.length > 0;
  if (strategy === 'vmware_warm') return passesSinceTheKitReleasedItsCopies(m).length > 0;
  return false;
}

/** Drop the cutover request with its requester and the window bypass granted with it (SDD §5.4). */
function clearRequest(m: Migration): void {
  m.cutover_requested = false;
  m.cutover_requested_by = null;
  m.cutover_requested_at = null;
  m.force_window = false;
  // a Force cutover belongs to the request too (ADV-16)
  m.force_cutover = null;
}

/** The API's `_probe_problems` (SDD §12, ADV-18): the application probes it refuses, with its words. */
function probeProblemsOf(where: string, value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const probes = value as Array<Partial<AppProbe>>;
  const problems = probes.filter((p) => !PROBE_KINDS.includes(p.kind as ProbeKind)).map((p) => `${where}: kind ${JSON.stringify(p.kind)} is not one of tcp, http, https`);
  problems.push(...probes.filter((p) => typeof p.port !== 'number' || p.port < 1 || p.port > 65535).map((p) => `${where}: port ${String(p.port)} outside 1-65535`));
  problems.push(
    ...probes
      .filter((p) => p.kind !== 'tcp' && typeof p.path === 'string' && (!p.path.startsWith('/') || p.path.length > MAX_PROBE_PATH))
      .map((p) => `${where}: path ${JSON.stringify(p.path)} must start with '/' (at most ${MAX_PROBE_PATH} characters)`),
  );
  problems.push(
    ...probes
      .filter((p) => typeof p.expect_status === 'number' && (p.expect_status < 100 || p.expect_status > 599))
      .map((p) => `${where}: expect_status ${String(p.expect_status)} outside 100-599`),
  );
  if (probes.length > MAX_PROBES_PER_VM) problems.push(`${where}: ${probes.length} probes, at most ${MAX_PROBES_PER_VM}`);
  return problems;
}

/** The API's `PLAN_EDITABLE_FIELDS` (SDD §12): what a `PlanCreate` and a PATCH may carry. */
export const PLAN_EDITABLE_FIELDS: ReadonlySet<string> = new Set([
  ...Object.keys(defaultPlanFields(0)).filter((key) => !['waves', 'derived_status', 'migration_counts', 'created_at', 'updated_at'].includes(key)),
  'name',
  'source_provider_id',
  'destination_provider_id',
  'vm_ids',
]);

/** Python's `repr` of a list of numbers, as the API's messages print one. */
function pyList(values: number[]): string {
  return `[${values.join(', ')}]`;
}

/**
 * The API's `check_plan_spec` after the text lengths (SDD §12), on the plan it would store: a VM listed twice, then
 * the settings that would fail every verification or never open the cutover gate (`invalid_plan_settings`), in its
 * order and words.
 */
/** What a plan may pre-stage, as the API's PRESTAGE_RESOURCES (SDD §4.2): the collection's playbook pairs, never workloads. */
const PRESTAGE_RESOURCES = [
  'networks', 'subnets', 'routers', 'router_interfaces', 'security_groups', 'security_group_rules',
  'flavors', 'keypairs', 'images', 'projects', 'users', 'users_keypairs', 'user_project_role_assignments', 'detached_volumes',
];

/** The plan model's bounds, in its field order, as pydantic words them (SDD §4.2, §12, review RV52-D4). */
const PLAN_BOUNDS: readonly (readonly [string, '>=' | '>', number])[] = [
  ['downtime_slo_s', '>=', 1],
  ['keep_warm_interval_s', '>=', 60],
  ['convergence_threshold_bytes', '>=', 0],
  ['max_sync_passes', '>=', 1],
  ['link_bps', '>', 0],
  ['finalize_warning_h', '>=', 1],
];
/** The estimator's bounds (SDD §9.1, reviews RV51-07, RV51-22): passes at most, a rate at least (B/s), a duration at most (s). */
const MAX_SYNC_PASSES = 100;
const MIN_RATE_BPS = 1;
const MAX_DURATION_S = 10 ** 9;
const OVERRIDE_RATES: ReadonlySet<string> = new Set(['link_bps', 'scan_bps', 'max_aggregate_scan_bps']);
const OVERRIDE_DURATIONS: ReadonlySet<string> = new Set(['shutdown_s', 'boot_s', 'create_s', 'snapshot_s', 'handover_per_volume_s', 'v2v_s', 'v2v_inplace_s']);
const OVERRIDE_PLAN_FIELDS: Readonly<Record<string, string>> = { convergence_threshold_bytes: 'convergence_threshold_bytes', max_passes: 'max_sync_passes' };

/** The API's invalid_estimator_overrides (SDD §9.1): one message per key, in sorted order. */
function overrideProblems(overrides: Record<string, unknown>): string[] {
  return Object.keys(overrides)
    .sort()
    .flatMap((key) => {
      const value = overrides[key];
      if (!Object.hasOwn(ESTIMATOR_DEFAULTS, key)) return [`${key}: unknown field`];
      if (Object.hasOwn(OVERRIDE_PLAN_FIELDS, key)) return [`${key}: set the plan field ${OVERRIDE_PLAN_FIELDS[key]} instead`];
      if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return [`${key}: must be a finite positive number`];
      if (key === 'parallel_disks' && Math.trunc(value) < 1) return [`${key}: must be at least 1`];
      if (OVERRIDE_RATES.has(key) && value < MIN_RATE_BPS) return [`${key}: must be at least 1 B/s`];
      if (OVERRIDE_DURATIONS.has(key) && value > MAX_DURATION_S) return [`${key}: must be at most ${MAX_DURATION_S} s`];
      return [];
    });
}

function refusePlanSpec(spec: Record<string, unknown>): void {
  // the model validates the body first (pydantic's words, five at most), then the API's checks run in their order
  const bounds = PLAN_BOUNDS.flatMap(([field, op, limit]) => {
    const value = spec[field];
    if (typeof value !== 'number' || (op === '>=' ? value >= limit : value > limit)) return [];
    return [`${field}: Input should be greater than ${op === '>=' ? 'or equal to ' : ''}${limit}`];
  });
  if (bounds.length) throw new HttpError(422, 'validation_error', bounds.slice(0, 5).join('; '));
  const ids = Array.isArray(spec.vm_ids) ? (spec.vm_ids as string[]) : [];
  const counts = new Map<string, number>();
  for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1);
  const repeated = [...counts].filter(([, n]) => n > 1).map(([id]) => id).sort();
  if (repeated.length) throw new HttpError(422, 'validation_error', `vm_ids lists a VM more than once: ${repeated.slice(0, 10).join(', ')}`);
  const verification = (spec.verification ?? {}) as { tcp_ports?: unknown; windows_tcp_ports?: unknown; timeout_s?: unknown; app_probes?: unknown };
  const problems: string[] = [];
  for (const name of ['tcp_ports', 'windows_tcp_ports'] as const) {
    const ports = Array.isArray(verification[name]) ? (verification[name] as number[]) : [];
    const bad = ports.filter((port) => !(port >= 1 && port <= 65535));
    if (bad.length) problems.push(`verification.${name} ${pyList(bad)} outside 1-65535`);
  }
  if (typeof verification.timeout_s === 'number' && verification.timeout_s < 0) {
    problems.push(`verification.timeout_s must not be negative (got ${verification.timeout_s})`);
  }
  problems.push(...probeProblemsOf('verification.app_probes', verification.app_probes));
  const overrides = (spec.probe_overrides ?? {}) as Record<string, unknown>;
  problems.push(...Object.entries(overrides).flatMap(([vmId, probes]) => probeProblemsOf(`probe_overrides[${vmId}]`, probes)));
  // like the API (SDD §12): what makes a NIC override unusable, named by VM and MAC
  const nicOverrides = (spec.nic_overrides ?? {}) as Record<string, Record<string, { network?: string | null; fixed_ips?: string[] | null }>>;
  for (const [vmId, nics] of Object.entries(nicOverrides)) {
    for (const mac of Object.keys(nics ?? {}).sort()) {
      const entry = nics[mac] ?? {};
      const where = `nic_overrides[${vmId}][${mac}]`;
      const network = entry.network ?? null;
      const ips = entry.fixed_ips ?? null;
      if (!/^[0-9a-f]{2}(:[0-9a-f]{2}){5}$/.test(mac)) problems.push(`${where}: not a MAC address`);
      if (network === null && ips === null) problems.push(`${where}: give a network, addresses or both`);
      if (network !== null && !network.trim()) problems.push(`${where}: network must not be empty`);
      if (ips === null) continue;
      if (!ips.length) problems.push(`${where}: fixed_ips must list at least one address (leave it out to keep the NIC's own)`);
      const bad = ips.filter((a) => !isIpAddress(a));
      if (bad.length) problems.push(`${where}: not an IP address: ${bad.join(', ')}`);
      const twice = [...new Set(ips.filter((a, i) => ips.indexOf(a) !== i))].sort();
      if (twice.length) problems.push(`${where}: ${twice.join(', ')} given twice`);
    }
  }
  const prestage = Array.isArray(spec.prestage_resources) ? (spec.prestage_resources as string[]) : [];
  const unknown = prestage.filter((r) => !PRESTAGE_RESOURCES.includes(r));
  if (unknown.length) problems.push(`prestage_resources: cannot pre-stage ${unknown.map((r) => `'${r}'`).join(', ')}`);
  if (spec.port_creation_mode !== undefined && !['nova', 'neutron'].includes(spec.port_creation_mode as string))
    problems.push("port_creation_mode: Input should be 'nova' or 'neutron'");
  const window = spec.cutover_window as { start?: string; end?: string } | null | undefined;
  if (window?.start && window.end && Date.parse(window.end) <= Date.parse(window.start)) problems.push('cutover_window: end must be after start');
  // a rate the estimator divides by, and a pass it runs per allowed pass (SDD §9.1, reviews RV51-07, RV51-22)
  if (typeof spec.link_bps === 'number') {
    if (!Number.isFinite(spec.link_bps)) problems.push(`link_bps must be a finite positive number (got ${pyFloat(spec.link_bps)})`);
    else if (spec.link_bps < MIN_RATE_BPS) problems.push(`link_bps must be at least 1 B/s (got ${pyFloat(spec.link_bps)})`);
  }
  if (typeof spec.max_sync_passes === 'number' && spec.max_sync_passes > MAX_SYNC_PASSES) {
    problems.push(`max_sync_passes must be at most ${MAX_SYNC_PASSES} (got ${spec.max_sync_passes})`);
  }
  if (problems.length) throw new HttpError(422, 'validation_error', problems.join('; '));
  const estimator = overrideProblems((spec.estimator_overrides ?? {}) as Record<string, unknown>);
  if (estimator.length) throw new HttpError(400, 'bad_request', `invalid estimator_overrides: ${estimator.join('; ')}`);
}

/**
 * Like the API's model (SDD §4.2): NIC override keys are stored lower case, each entry with both fields and its valid
 * addresses in their canonical form ("FD00:0::0005" is "fd00::5"), before anything is checked.
 */
function withCanonicalNicOverrides(input: Record<string, unknown>): Record<string, unknown> {
  if (!input.nic_overrides || typeof input.nic_overrides !== 'object') return input;
  const overrides = input.nic_overrides as Record<string, Record<string, { fixed_ips?: unknown }>>;
  const canonical = (entry: { fixed_ips?: unknown }) =>
    Array.isArray(entry.fixed_ips) ? { ...entry, fixed_ips: entry.fixed_ips.map((a) => (typeof a === 'string' ? canonicalIp(a) : a)) } : entry;
  return {
    ...input,
    nic_overrides: Object.fromEntries(
      Object.entries(overrides).map(([vm, nics]) => [
        vm,
        Object.fromEntries(Object.entries(nics ?? {}).map(([mac, entry]) => [mac.toLowerCase(), { network: null, fixed_ips: null, ...canonical(entry ?? {}) }])),
      ]),
    ),
  };
}

/**
 * The simulated guest's answer to an application probe, like the demo's fake destination (SDD §7.4):
 * it runs its suggested services and SSH (RDP on Windows); `demo-fault: app-down` runs none of its own.
 */
function simulatedProbe(vm: VMRef, probe: AppProbe, address: string, at: string): ProbeResult {
  const listening = new Set([guestOsOf(vm).family === 'windows' ? 3389 : 22]);
  if (vm.tags['demo-fault'] !== 'app-down') for (const p of suggestProbes(vm)) listening.add(p.port);
  const target = probe.kind === 'tcp' ? `${address}:${probe.port}` : `${probe.kind}://${address}:${probe.port}${probe.path}`;
  if (!listening.has(probe.port)) return { target, kind: probe.kind, result: 'failed', latency_ms: null, at, detail: `${address}:${probe.port} refused the connection` };
  if (probe.kind === 'tcp') return { target, kind: probe.kind, result: 'passed', latency_ms: 3, at, detail: `connected to ${address}:${probe.port}` };
  const ok = probe.expect_status === null || probe.expect_status === 200;
  return { target, kind: probe.kind, result: ok ? 'passed' : 'failed', latency_ms: 3, at, detail: ok ? 'HTTP 200' : `HTTP 200, expected ${probe.expect_status}` };
}

/** The phase-history reason of a rule, like the API's _converged (SDD §5.3). */
function convergedReason(m: Migration, plan: Plan, rule: ConvergedBy): string {
  switch (rule) {
    case 'force':
      return `Force cutover by ${m.force_cutover?.actor ?? 'an approver'}: ${m.force_cutover?.reason ?? ''}`;
    case 'threshold':
      return 'converged: last pass below the threshold';
    case 'slo':
      return 'converged: the estimated downtime fits the SLO';
    case 'max_passes':
      return `max_sync_passes (${plan.max_sync_passes}) reached`;
  }
}

/**
 * The rule that ends the passes after `pass`, in the API's order (SDD §5.3, §5.4): a Force cutover, the
 * threshold (not for CBT passes), the pass limit. The mock leaves out the SLO rule: its passes run in
 * seconds, which would converge every VM after its full copy and leave the chart without deltas.
 */
function convergedBy(m: Migration, plan: Plan, pass: SyncPass): ConvergedBy | null {
  if (m.force_cutover) return 'force';
  if (m.strategy !== 'vmware_warm' && pass.bytes_changed <= plan.convergence_threshold_bytes) return 'threshold';
  return m.sync_passes.length >= plan.max_sync_passes ? 'max_passes' : null;
}

/** knowledge.size_bucket: the profile bucket a memory query names (SDD §14.3). */
function sizeBucket(diskBytes: number): string {
  const gib = diskBytes / 2 ** 30;
  if (gib < 50) return '<50G';
  if (gib < 200) return '50-200G';
  return gib <= 500 ? '200-500G' : '>500G';
}

/** The part of a policy decide() needs (SDD §14.4, §14.6). */
interface PolicyRule {
  id: string;
  mode: AdvisorPolicy['mode'];
  threshold: number;
  min_evidence_for_auto_apply: number;
  require_override_reason: boolean;
  require_distinct_approver: boolean;
}

const GLOBAL_DEFAULT: PolicyRule = {
  id: 'global-default',
  mode: 'auto_apply_above_threshold',
  threshold: JEV_MIN_CONFIDENCE,
  min_evidence_for_auto_apply: 0,
  require_override_reason: false,
  require_distinct_approver: true,
};

/** The policy in force for a plan's strategy decisions (SDD §14.6), or the built-in default. */
function policyFor(policies: readonly AdvisorPolicy[], plan: Plan): PolicyRule {
  const winner = resolvePolicy(policies, plan.id, plan.source_provider_id, 'migration_strategy');
  return winner
    ? {
        id: winner.id,
        mode: winner.mode,
        threshold: winner.threshold,
        min_evidence_for_auto_apply: winner.min_evidence_for_auto_apply,
        require_override_reason: winner.require_override_reason,
        require_distinct_approver: winner.require_distinct_approver,
      }
    : GLOBAL_DEFAULT;
}

/**
 * decide() (SDD §14.4): no usable advice, a policy that asks a person, below the threshold, fewer facts that
 * support or oppose than the policy's minimum (ADV-02), else applied.
 */
function decided(
  advice: SimulatedAdvice,
  scope: readonly Strategy[],
  policy: PolicyRule,
  evidence: number,
): { status: StrategyDecision; fallback?: string; reason?: ReasonCode } {
  if (advice.option === null) return { status: 'fallback_rules', fallback: 'advisor unavailable: Jev gave no answer' };
  if (!scope.includes(advice.option as Strategy)) {
    return { status: 'fallback_rules', fallback: `${advice.option} is not among the options it may choose (${scope.join(', ')})` };
  }
  if (policy.mode === 'always_require_approval') return { status: 'pending_approval' };
  if (advice.confidence === null || advice.confidence < policy.threshold) return { status: 'below_threshold' };
  if (evidence < policy.min_evidence_for_auto_apply) return { status: 'pending_approval', reason: 'INSUFFICIENT_EVIDENCE' };
  return { status: 'auto_applied' };
}

/** Strategy notes of earlier versions read as recommendations, like the API's listing: nothing is copied (SDD §14.4). */
function legacyRecommendations(m: Migration): AdvisorRecommendation[] {
  return m.advisor_notes
    .filter((note) => note.kind === 'strategy')
    .map((note, index): AdvisorRecommendation => {
      const data = note.data;
      const option = typeof data.selected === 'string' ? data.selected : null;
      const deterministic = typeof data.deterministic === 'string' ? data.deterministic : null;
      const probabilities = typeof data.probabilities === 'object' && data.probabilities !== null ? (data.probabilities as Record<string, unknown>) : {};
      const [status, reason_code, final_option] =
        data.applied === true && option !== null
          ? (['auto_applied', 'ABOVE_THRESHOLD_AUTO_APPLY', option] as const)
          : note.source === 'rules'
            ? (['fallback_rules', 'ADVISOR_UNAVAILABLE_FALLBACK', deterministic] as const)
            : (['below_threshold', 'BELOW_THRESHOLD_DEFAULT_KEPT', deterministic] as const);
      return {
        id: `rec-legacy-${m.id}-${index}`,
        created_at: note.created_at,
        decision_type: 'migration_strategy',
        subject: { kind: 'vm', id: m.vm.source_id, name: m.vm.name },
        plan_id: m.plan_id,
        migration_id: m.id,
        provider_id: null,
        model: note.source === 'jev' ? { name: 'jev', version: 'unknown', deployment: 'unknown', endpoint_host: null } : { name: 'rules', version: 'rules', deployment: 'unknown', endpoint_host: null },
        recommendation: {
          option,
          confidence: note.confidence,
          distribution: Object.fromEntries(Object.entries(probabilities).filter((entry): entry is [string, number] => typeof entry[1] === 'number')),
          unusable: null,
        },
        decision: { final_option, status, reason_code, reason_text: null, actor: { kind: 'system', id: 'legacy' }, policy_id: null, threshold_used: null, decided_at: note.created_at },
        input_snapshot_sha256: null,
        requested_by: null,
        legacy: true,
        legacy_text: note.summary,
        evidence_option: null,
        evidence: [],
        similar_incidents: [],
        reasons: [],
      };
    });
}

/** An integer query parameter the API validates (422 outside `min`..`max`, SDD §12). */
function intParam(query: URLSearchParams, name: string, fallback: number, min: number, max = Number.MAX_SAFE_INTEGER): number {
  const raw = query.get(name);
  if (raw === null) return fallback;
  const value = Number(raw);
  // the API's words for a query parameter it refuses (FastAPI's validation, SDD §12)
  const refuse = (why: string) => new HttpError(422, 'validation_error', `query.${name}: Input should be ${why}`);
  if (!/^-?\d+$/.test(raw.trim())) throw refuse('a valid integer, unable to parse string as an integer');
  if (value < min) throw refuse(`greater than or equal to ${min}`);
  if (value > max) throw refuse(`less than or equal to ${max}`);
  return value;
}

/** Phases in which a migration lets its VM go: another plan may migrate it (SDD §5.4), as in the API. */
const RELEASES_VM: ReadonlySet<Phase> = new Set(['cancelled', 'finalized', 'rolled_back']);
/** The API's refusal of a finalize while the source watch sees the source VM running again (SDD §8.1). */
const SOURCE_RUNS_AGAIN = 'the source VM runs again: stop it, or roll it back, before finalizing';
/** Phases in which a step runs for the migration (the API's ACTIVE_STEP_PHASES): they fill a wave's max_parallel. */
const ACTIVE_STEP_PHASES: ReadonlySet<Phase> = new Set(['precopy', 'syncing', 'cutover', 'verifying', 'rolling_back']);
/** Phases validation evaluates again; a VM taken out of the plan has its migration cancelled in these. */
const REVALIDATABLE: ReadonlySet<Phase> = new Set(['pending', 'blocked', 'ready']);
/** Phases a validation re-assesses, one an interrupted validation left in validating included (the API's REASSESSED). */
const REASSESSED: ReadonlySet<Phase> = new Set([...REVALIDATABLE, 'validating']);
/**
 * Whether a validation re-assesses `m`, the API's reassessed() (SDD §5.4, audit OC-02, reviews RV51-01, RV51-09): unless it
 * is ready with its source stopped - a re-assessment could block it, where no action restarts the source -; one already
 * blocked or left in validating is re-assessed whatever its source, a validation being its way back to ready.
 */
function reassessed(m: Migration): boolean {
  return REASSESSED.has(m.phase) && !(m.phase === 'ready' && sourceStopped(m));
}
/** Phases whose migration validation cancels when its VM leaves vm_ids, like the API (SDD §5.4). */
// a migration found in validating is a leftover of an interrupted validation (SDD §5.4)
const REMOVABLE: ReadonlySet<Phase> = new Set([...REVALIDATABLE, 'failed', 'validating']);
/** At most `limit` holders from `heldElsewhere`, and how many are left out (the API's wording). */
function holdersText(held: Map<string, string[]>, limit = 10): string {
  const entries = [...held.values()].flat().sort();
  const rest = entries.length - limit;
  return entries.slice(0, limit).join(', ') + (rest > 0 ? ` and ${rest} more` : '');
}
/** Drop the oldest passes between the first `keepFirst` and the latest 20 (SDD §5.4, as in the API); their bytes move to `sync_bytes_dropped`. */
function keepSyncHistory(m: Migration, keepFirst: number): void {
  const excess = m.sync_passes.length - keepFirst - SYNC_PASSES_LATEST;
  if (excess <= 0) return;
  const dropped = m.sync_passes.splice(keepFirst, excess);
  m.sync_bytes_dropped += dropped.reduce((sum, p) => sum + p.bytes_transferred, 0);
}
/** Plan fields that decide whether a cutover needs a human (SDD §12): approver-only. */
const POLICY_FIELDS = ['require_approval', 'require_distinct_approver', 'auto_cutover', 'cutover_window'] as const;
/** A secret name the control plane reads (SDD §13.3): never a path. */
const SECRET_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

/** Like the API: a named secret must be readable; only the fields the request sets are checked (SDD §13.3). */
function checkSecretNames(input: Record<string, unknown>, fields: Set<string>, providerId: string): void {
  const names: Array<[string, unknown]> = [];
  if (fields.has('credentials_secret')) names.push(['credentials_secret', input.credentials_secret]);
  const host = input.conversion_host as { ssh_key_secret?: unknown } | null | undefined;
  if (fields.has('conversion_host') && host) names.push(['conversion_host.ssh_key_secret', host.ssh_key_secret]);
  const bad = names.filter(([, name]) => name !== null && name !== undefined && !(typeof name === 'string' && SECRET_NAME.test(name))).map(([field]) => field);
  if (bad.length) {
    throw new HttpError(422, 'validation_error', `${bad.join(', ')}: a secret name is 1-128 letters, digits, '.', '_' or '-', starting with a letter or digit`);
  }
  // provider-* names are the secrets Seamless stores for each provider: only the provider's own (SDD §13.3)
  const own: Record<string, string> = { credentials_secret: `provider-${providerId}`, 'conversion_host.ssh_key_secret': `provider-${providerId}-ssh` };
  const taken = names.filter(([field, name]) => typeof name === 'string' && name.startsWith('provider-') && name !== own[field]).map(([field]) => field);
  if (taken.length) {
    throw new HttpError(422, 'validation_error', `${taken.join(', ')}: provider-* names belong to the secrets Seamless stores for each provider; this one is another provider's`);
  }
}

const PROVIDER_EDITABLE = new Set(['name', 'endpoint', 'cloud', 'credentials_secret', 'region', 'verify_tls', 'ca_cert_path', 'conversion_host', 'distribution', 'release', 'policy_profile']);
const DISTRIBUTION_KIND: Record<string, string> = { openstack_community: 'openstack', kolla: 'openstack', rhosp: 'openstack', rhoso: 'rhoso', vmware: 'vmware' };

function checkDistribution(input: Record<string, unknown> | Provider) {
  const distribution = (input as Record<string, unknown>).distribution;
  const kind = (input as Record<string, unknown>).kind;
  if (distribution && DISTRIBUTION_KIND[String(distribution)] !== kind) {
    throw new HttpError(422, 'validation_error', `distribution ${String(distribution)} belongs to kind ${DISTRIBUTION_KIND[String(distribution)] ?? 'unknown'}, not ${String(kind)}`);
  }
}

/** A declared series and policy profile, as the API's Provider validates them (SDD §4.2): OpenStack's alone. */
function checkDeclarations(input: Record<string, unknown> | Provider) {
  const fields = input as Record<string, unknown>;
  const release = fields.release;
  if (release !== undefined && release !== null && !(typeof release === 'string' && /^20[0-9]{2}\.[12]$/.test(release))) {
    throw new HttpError(422, 'validation_error', "release: String should match pattern '^20[0-9]{2}\\.[12]$'");
  }
  const profile = fields.policy_profile;
  if (profile !== undefined && profile !== null && profile !== 'secure_rbac' && profile !== 'legacy') {
    throw new HttpError(422, 'validation_error', "policy_profile: Input should be 'secure_rbac' or 'legacy'");
  }
  if (fields.kind === 'vmware') {
    for (const name of ['release', 'policy_profile']) {
      if (fields[name] !== undefined && fields[name] !== null) throw new HttpError(422, 'validation_error', `Value error, ${name}: an OpenStack ${name} cannot be declared for vmware`);
    }
  }
}

export class MockServer {
  private readonly users = new Map<string, Me>();
  /** What GET /health answers; tests flip it to a degraded state. */
  health: Health = {
    status: 'ok',
    version: '0.1.0-mock',
    demo: true,
    // the simulation's model time is its wall time: passes, downtimes and clocks share one base (ADV-16)
    demo_speed: 1,
    // no token is the anonymous admin, as with authentication off (SDD §13.1)
    auth_disabled: true,
    db: 'ok',
    orchestrator: { running: true, last_tick_age_s: 0.4, ticks: 1200, healthy: true },
  };
  readonly providers: Provider[];
  readonly inventories: Record<string, VMRef[] | DestinationInventory>;
  readonly plans: Plan[];
  readonly migrations: Migration[];
  /** Stored recommendations; strategy notes of earlier versions are listed beside them (SDD §14.4). */
  readonly recommendations: AdvisorRecommendation[];
  /** Redacted advisor inputs by SHA-256, written once (SDD §14.5). */
  readonly snapshots: Map<string, AdvisorSnapshot>;
  /** Stored advisor policies (SDD §14.6). */
  readonly policies: AdvisorPolicy[];
  readonly events: Event[];
  /** Source power states the demo set (POST /demo/vms/…/power, SDD §8.1). */
  private readonly power = new Map<string, 'running' | 'stopped'>();
  private seq: number;
  private readonly advise: NonNullable<MockServerOptions['advise']>;
  private readonly listeners = new Set<(event: Event) => void>();
  private readonly rand: () => number;
  private readonly now: () => number;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** The pass each migration is running: like the API, sync_passes lists a pass only once it ends (SDD §4.2). */
  private readonly running = new Map<string, SyncPass>();
  /** The bytes each pass moves (`<migration id>#<pass number>`), drawn once per pass. */
  private readonly passSizes = new Map<string, number>();
  /** The request being handled, for the `auth.denied` audit (SDD §13.1). */
  private request = { method: 'GET', path: '/' };

  constructor(options: MockServerOptions = {}) {
    this.now = options.now ?? (() => Date.now());
    this.rand = prng(options.seed ?? 42);
    const fixtures = buildFixtures(this.now());
    this.providers = fixtures.providers;
    // by own key only: a provider id is any ^[a-z0-9][a-z0-9-]{1,62}$, "constructor" too, never Object.prototype's
    // (SDD §4.2, review RV22-02)
    this.inventories = Object.assign(Object.create(null) as Record<string, VMRef[] | DestinationInventory>, fixtures.inventories);
    this.plans = fixtures.plans;
    this.migrations = fixtures.migrations;
    this.recommendations = fixtures.recommendations;
    this.snapshots = new Map(fixtures.snapshots.map((snapshot) => [snapshot.sha256, snapshot]));
    this.policies = fixtures.policies;
    this.events = fixtures.events;
    this.seq = this.events.at(-1)?.seq ?? 0;
    this.advise = options.advise ?? simulatedAdvice;
  }

  // ---------------------------------------------------------------------------------------
  // Simulation clock
  // ---------------------------------------------------------------------------------------

  start(intervalMs = 1000): void {
    if (this.timer) return;
    this.timer = setInterval(() => this.tick(), intervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  subscribe(listener: (event: Event) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Number of connected event-stream subscribers (lets tests wait for the SSE connection). */
  get subscriberCount(): number {
    return this.listeners.size;
  }

  /** Publish an event to stream subscribers; persisted events get the next sequence number. */
  emit(partial: Omit<Event, 'seq' | 'ts'> & { ts?: string }, persisted = true): Event {
    const event: Event = { ...partial, seq: persisted ? ++this.seq : 0, ts: partial.ts ?? new Date(this.now()).toISOString() };
    if (persisted) {
      this.events.push(event);
      if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);
    }
    for (const listener of this.listeners) listener(event);
    return event;
  }

  /** One simulation step (1 s of demo time). */
  tick(): void {
    this.watchSources();
    const now = this.now();
    for (const m of this.migrations) {
      const plan = this.plans.find((p) => p.id === m.plan_id);
      if (!plan) continue;
      const enteredAt = Date.parse(m.phase_history.at(-1)?.at ?? m.updated_at);
      const inPhaseS = (now - enteredAt) / 1000;
      switch (m.phase) {
        case 'validating':
          if (inPhaseS >= 3) this.finishValidation(m, plan, 'orchestrator');
          break;
        case 'precopy':
        case 'syncing':
          this.advanceTransfer(m, plan, m.phase === 'precopy' ? 1.4 : 4);
          break;
        case 'ready':
          // like the API's tick and _begin (SDD §5.4, §8): no held migration, only an active wave with room
          if (plan.status !== 'running' || !this.mayBegin(m, plan)) break;
          if (SINGLE_SHOT.has(m.strategy)) {
            if (this.gateOpen(m, plan)) this.beginCutover(m, plan);
          } else {
            this.transition(m, 'precopy', 'pre-copy started', 'orchestrator');
          }
          break;
        case 'awaiting_cutover':
          if (plan.status === 'running' && this.mayBegin(m, plan) && this.gateOpen(m, plan)) this.beginCutover(m, plan);
          break;
        case 'cutover':
          this.advanceCutover(m, plan);
          break;
        case 'verifying':
          if (inPhaseS >= 12) this.finishVerification(m, plan);
          break;
        case 'rolling_back':
          m.progress_pct = Math.min(100, m.progress_pct + 6);
          this.stepProgress(m, 0, 0);
          if (m.progress_pct >= 100) {
            this.transition(m, 'rolled_back', 'source VM running again', 'orchestrator');
            this.endDowntime(m);
          }
          break;
        default:
          break;
      }
    }
  }

  /**
   * The running step's progress, like the API (SDD §4.2, §4.3): the migration's bytes_transferred is
   * every pass that ended plus the step's bytes, and the ephemeral event carries the step's own figures.
   */
  private stepProgress(m: Migration, done: number, total: number): void {
    m.bytes_transferred = endedPassBytes(m) + done;
    this.emit(
      {
        kind: 'migration.progress',
        plan_id: m.plan_id,
        migration_id: m.id,
        actor: 'orchestrator',
        message: `${m.vm.name}: ${m.progress_pct.toFixed(1)}%`,
        data: { pct: Math.round(m.progress_pct * 100) / 100, bytes_done: done, bytes_total: total, phase: m.phase },
      },
      false,
    );
  }

  private openPass(m: Migration): SyncPass | undefined {
    return this.running.get(m.id);
  }

  /** The running pass of a migration the fixtures start mid-step: it continues from the bytes it moved. */
  private resumePass(m: Migration, kind: SyncPass['kind']): SyncPass {
    const pass: SyncPass = {
      number: (m.sync_passes.at(-1)?.number ?? 0) + 1,
      kind,
      started_at: m.phase_history.at(-1)?.at ?? m.updated_at,
      ended_at: null,
      bytes_scanned: Math.round((m.vm.disk_bytes * m.progress_pct) / 100),
      bytes_changed: 0,
      bytes_transferred: Math.max(0, m.bytes_transferred - endedPassBytes(m)),
      duration_s: null,
    };
    this.running.set(m.id, pass);
    return pass;
  }

  /** A step that ends without its pass (a rollback, a cancel) records nothing, like the API. */
  private dropRunningPass(m: Migration): void {
    const pass = this.running.get(m.id);
    if (!pass) return;
    this.running.delete(m.id);
    this.passSizes.delete(`${m.id}#${pass.number}`);
  }

  private startPass(m: Migration, kind: SyncPass['kind']): SyncPass {
    const pass: SyncPass = {
      number: (m.sync_passes.at(-1)?.number ?? 0) + 1,
      kind,
      started_at: new Date(this.now()).toISOString(),
      ended_at: null,
      bytes_scanned: 0,
      bytes_changed: 0,
      bytes_transferred: 0,
      duration_s: null,
    };
    this.running.set(m.id, pass);
    m.progress_pct = 0;
    return pass;
  }

  private passTarget(m: Migration, kind: SyncPass['kind']): number {
    if (kind === 'full') return m.vm.used_bytes;
    const previous = [...m.sync_passes].reverse().find((p) => p.ended_at !== null);
    const base = previous ? previous.bytes_changed : m.vm.used_bytes;
    return Math.max(32 * MiB, Math.round(base * (0.18 + this.rand() * 0.12)));
  }

  private advanceTransfer(m: Migration, plan: Plan, pctPerTick: number): void {
    const kind = m.sync_passes.length === 0 ? 'full' : 'delta';
    const pass = this.openPass(m) ?? (m.progress_pct > 0 ? this.resumePass(m, kind) : this.startPass(m, kind));
    // a pass transfers 98 % of the bytes it finds changed (zeroed blocks are skipped)
    const moved = this.passSize(m, pass, 0.98);
    m.progress_pct = Math.min(100, m.progress_pct + pctPerTick * (0.7 + this.rand() * 0.6));
    const done = Math.round((moved * m.progress_pct) / 100);
    pass.bytes_scanned = Math.round((m.vm.disk_bytes * m.progress_pct) / 100);
    pass.bytes_transferred = done;
    m.updated_at = new Date(this.now()).toISOString();
    this.stepProgress(m, done, moved);
    if (m.progress_pct < 100) return;

    const now = this.now();
    pass.ended_at = new Date(now).toISOString();
    pass.duration_s = Math.round((now - Date.parse(pass.started_at)) / 100) / 10;
    pass.bytes_scanned = m.vm.disk_bytes;
    pass.bytes_changed = Math.round(moved / 0.98);
    pass.bytes_transferred = moved;
    m.sync_passes.push(pass);
    this.running.delete(m.id);
    this.passSizes.delete(`${m.id}#${pass.number}`);
    keepSyncHistory(m, plan.max_sync_passes);
    m.bytes_transferred = endedPassBytes(m);
    this.emit({
      kind: 'migration.sync_pass',
      plan_id: m.plan_id,
      migration_id: m.id,
      actor: 'orchestrator',
      message: `${m.vm.name}: pass ${pass.number} (${pass.kind}) changed ${(pass.bytes_changed / GiB).toFixed(2)} GiB`,
      data: { ...pass },
    });
    m.checkpoint = m.phase === 'precopy' ? 'precopy' : 'sync';
    const rule = convergedBy(m, plan, pass);
    if (rule) {
      // like the API's _converge (SDD §5.3): the rule is recorded, from pre-copy too
      m.converged_by = rule;
      m.progress_pct = 100;
      this.transition(m, 'awaiting_cutover', convergedReason(m, plan, rule), 'orchestrator');
    } else if (m.phase === 'precopy') {
      this.transition(m, 'syncing', 'pass 1 complete; delta passes until convergence', 'orchestrator');
      this.startPass(m, 'delta');
    } else {
      this.startPass(m, 'delta');
    }
  }

  /**
   * Like the API's tick and _begin (SDD §5.4, §8): a migration starts a step only when no advisor decision holds it
   * and its wave is active (every wave it depends on complete) - from ready also only with room under its wave's
   * max_parallel.
   */
  private mayBegin(m: Migration, plan: Plan): boolean {
    if (m.advisor_hold || !isWaveActive(plan, m.wave_id, this.migrations)) return false;
    const wave = m.phase === 'ready' ? plan.waves.find((w) => w.id === m.wave_id) : undefined;
    if (!wave) return true;
    const running = this.migrations.filter((x) => x.plan_id === plan.id && x.wave_id === wave.id && ACTIVE_STEP_PHASES.has(x.phase)).length;
    return running < wave.max_parallel;
  }

  private gateOpen(m: Migration, plan: Plan): boolean {
    const approved = !plan.require_approval || countedApprovals(m, plan).length > 0;
    const requested = plan.auto_cutover || m.cutover_requested;
    const window = plan.cutover_window;
    const now = this.now();
    // like the API (SDD §5.4): only an approver's force_window lets a requested cutover start outside the window
    const inWindow = !window || (now >= Date.parse(window.start) && now <= Date.parse(window.end)) || m.force_window;
    const cutovers = this.migrations.filter((x) => x.phase === 'cutover').length;
    return approved && requested && inWindow && cutovers < 3;
  }

  private beginCutover(m: Migration, plan: Plan): void {
    this.transition(m, 'cutover', 'cutover gate satisfied', 'orchestrator');
    if (!m.downtime_started_at || m.downtime_ended_at) {
      // a retried cutover keeps the clock of the first stop (SDD §5.2): the clock starts only here
      m.downtime_started_at = new Date(this.now()).toISOString();
      m.downtime_ended_at = null;
      m.actual_downtime_s = null;
      this.emit({ kind: 'migration.downtime_started', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: source VM stopped — downtime clock started`, data: {} });
    }
    m.progress_pct = 0;
    // a cold copy is recorded as a full pass, as the API's executors record it
    if (m.strategy === 'warm' || m.strategy === 'vmware_warm') this.startPass(m, 'final');
    else if (m.strategy === 'cold' || m.strategy === 'vmware_cold') this.startPass(m, 'full');
  }

  /**
   * The bytes a pass transfers — `share` of the bytes it finds changed — drawn once, so a migration's
   * figure never drops within a pass (SDD §16); a pass the fixtures start part-way keeps what it moved.
   */
  private passSize(m: Migration, pass: SyncPass, share: number): number {
    const key = `${m.id}#${pass.number}`;
    let size = this.passSizes.get(key);
    if (size === undefined) {
      const partWay = pass.bytes_transferred > 0 && m.progress_pct > 0;
      size = partWay ? Math.round(pass.bytes_transferred / (m.progress_pct / 100)) : Math.round(this.passTarget(m, pass.kind) * share);
      this.passSizes.set(key, size);
    }
    return size;
  }

  private advanceCutover(m: Migration, plan: Plan): void {
    const rate = m.strategy === 'warm' || m.strategy === 'vmware_warm' ? 6 : m.strategy === 'storage_handover' ? 8 : 2.5;
    // the step's bytes: the final pass of a warm migration, the full pass of a cold copy, none for a handover
    const kind = m.strategy === 'warm' || m.strategy === 'vmware_warm' ? 'final' : m.strategy === 'storage_handover' ? null : 'full';
    const pass = this.openPass(m) ?? (kind === null ? undefined : m.progress_pct > 0 ? this.resumePass(m, kind) : this.startPass(m, kind));
    const total = pass ? this.passSize(m, pass, 1) : 0;
    m.progress_pct = Math.min(100, m.progress_pct + rate * (0.7 + this.rand() * 0.6));
    const done = Math.round((total * m.progress_pct) / 100);
    if (pass) {
      pass.bytes_scanned = Math.round((m.vm.disk_bytes * m.progress_pct) / 100);
      pass.bytes_transferred = done;
    }
    this.stepProgress(m, done, total);
    if (m.progress_pct < 100) return;
    if (pass) {
      this.passSizes.delete(`${m.id}#${pass.number}`);
      const now = this.now();
      pass.ended_at = new Date(now).toISOString();
      pass.duration_s = Math.round((now - Date.parse(pass.started_at)) / 100) / 10;
      pass.bytes_scanned = m.vm.disk_bytes;
      pass.bytes_changed = total;
      pass.bytes_transferred = total;
      m.sync_passes.push(pass);
      this.running.delete(m.id);
      keepSyncHistory(m, plan.max_sync_passes);
      m.bytes_transferred = endedPassBytes(m);
      this.emit({ kind: 'migration.sync_pass', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: ${pass.kind} pass changed ${(pass.bytes_changed / GiB).toFixed(2)} GiB`, data: { ...pass } });
    }
    m.destination_server_id = `${hex(this.rand, 8)}-${hex(this.rand, 4)}-4${hex(this.rand, 3)}-a${hex(this.rand, 3)}-${hex(this.rand, 12)}`;
    // like the simulated executor (SDD §5.2): the seconds the source stayed stopped for the step, here at wall speed
    if (m.downtime_started_at) m.downtime_model_s = Math.round((this.now() - Date.parse(m.downtime_started_at)) / 100) / 10;
    m.checkpoint = 'cutover';
    // like the control plane (SDD §7.2): the cutover got past what the passes said it would refuse
    m.cutover_refusals = [];
    this.transition(m, 'verifying', 'destination server created', 'orchestrator');
  }

  private finishVerification(m: Migration, plan: Plan): void {
    const at = new Date(this.now()).toISOString();
    // one verification in ten fails on the boot checks (an exercised rollback path), like the demo
    const flaky = this.rand() < 0.1;
    const windows = guestOsOf(m.vm).family === 'windows';
    const ports = windows ? plan.verification.windows_tcp_ports : plan.verification.tcp_ports;
    const configured = probesFor(m.vm.source_id, plan).probes;
    const address = m.vm.nics[0]?.fixed_ips[0] ?? '10.20.0.10';
    const probes = configured.map((probe) => simulatedProbe(m.vm, probe, address, at));
    const checks: VerificationCheck[] = [
      { name: 'server_active', ok: true, detail: 'status ACTIVE' },
      { name: 'ports_up', ok: true, detail: '1/1 ports ACTIVE' },
      ...ports.map((port) => ({ name: `tcp:${port}`, ok: !flaky, detail: flaky ? 'connection refused' : `connected to ${address}:${port}` })),
      ...configured.map((probe, i) => {
        const result = probes[i]!;
        return { name: `app:${probe.kind}:${probe.port}`, ok: result.result === 'passed', detail: result.detail, ...(result.result === 'unreachable' ? { skipped: true } : {}) };
      }),
      // a boot that fails without a TCP port to show it shows on the console
      ...(windows
        ? []
        : [flaky && ports.length === 0 ? { name: 'console', ok: false, detail: 'no success pattern in the last 200 console lines' } : { name: 'console', ok: true, detail: "matched 'login:'" }]),
    ];
    const refused = checks.filter((c) => !c.ok && !c.skipped);
    m.verification = { at, passed: refused.length === 0, checks, probes, warnings: [] };
    if (refused.length) {
      // a failed check — a boot check, or a refused application probe (SDD §7.5, ADV-18) — fails the
      // verification, and the plan's automatic rollback runs
      m.error = `Verification failed: ${refused.map((c) => `${c.name} (${c.detail})`).join('; ')}`;
      this.emit({ kind: 'migration.error', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: ${m.error}`, data: {} });
      this.transition(m, 'failed', 'verification failed', 'orchestrator', { verification: m.verification });
      if (flaky) {
        m.advisor_notes.push({
          kind: 'similar_incidents',
          source: 'memory',
          summary: '1 similar incident: sshd bound to the old fixed IP after migration.',
          confidence: null,
          data: { hits: [{ title: 'sshd ListenAddress pinned to the source IP', content: 'Remove ListenAddress or use 0.0.0.0; re-run verification.', score: 0.77 }] },
          created_at: new Date(this.now()).toISOString(),
        });
        this.emit({ kind: 'advisor.similar_incidents', plan_id: plan.id, migration_id: m.id, actor: 'agentmemory', message: `${m.vm.name}: 1 similar incident found`, data: {} });
      }
      if (plan.verification.auto_rollback) {
        m.progress_pct = 0;
        this.transition(m, 'rolling_back', 'automatic rollback after failed verification', 'orchestrator');
      }
      return;
    }
    // what the rules ask review for (SDD §7.5): an unreachable probe, a database VM that only booted
    const unreachable = probes.filter((p) => p.result === 'unreachable').map((p) => p.target);
    const review = unreachable.length
      ? `application probe unreachable from the control plane: ${unreachable.join(', ')}`
      : isDatabase(m.vm) && configured.length === 0
        ? 'database VM without an application probe: only its boot was verified'
        : null;
    if (review) {
      m.review_required = true;
      m.review_reason = review;
    }
    m.checkpoint = 'verified';
    this.transition(m, 'completed', 'verification passed', 'orchestrator', { verification: m.verification });
    this.endDowntime(m);
    this.emit({ kind: 'memory.lesson_saved', plan_id: plan.id, migration_id: m.id, actor: 'agentmemory', message: `Lesson saved: ${m.strategy}, actual downtime ${Math.round(m.actual_downtime_s ?? 0)} s`, data: { type: 'fact' } });
    this.maybeCompletePlan(plan);
  }

  private endDowntime(m: Migration): void {
    // only an open clock ends: a completed migration's verification closed its clock, which its rollback leaves
    // alone, like the API (SDD §5.2)
    if (!m.downtime_started_at || m.downtime_ended_at) return;
    const now = this.now();
    m.downtime_ended_at = new Date(now).toISOString();
    m.actual_downtime_s = Math.round((now - Date.parse(m.downtime_started_at)) / 100) / 10;
    this.emit({ kind: 'migration.downtime_ended', plan_id: m.plan_id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: downtime ended after ${Math.round(m.actual_downtime_s)} s`, data: { actual_downtime_s: m.actual_downtime_s } });
  }

  private maybeCompletePlan(plan: Plan): void {
    const mine = this.migrations.filter((m) => m.plan_id === plan.id);
    const done = mine.every((m) => ['completed', 'finalized', 'cancelled', 'rolled_back'].includes(m.phase));
    if (plan.status === 'running' && done) {
      plan.status = 'completed';
      plan.updated_at = new Date(this.now()).toISOString();
      this.emit({ kind: 'plan.completed', plan_id: plan.id, migration_id: null, actor: 'orchestrator', message: `Plan "${plan.name}" completed`, data: {} });
    }
  }

  // ---------------------------------------------------------------------------------------
  // Domain helpers
  // ---------------------------------------------------------------------------------------

  private transition(m: Migration, to: Phase, reason: string, actor: string, data: Record<string, unknown> = {}): void {
    if (!canTransition(m.phase, to)) {
      throw new HttpError(409, 'conflict', `Cannot move ${m.vm.name} from ${m.phase} to ${to}.`);
    }
    const at = new Date(this.now()).toISOString();
    m.phase_history.push({ from_phase: m.phase, to_phase: to, at, reason, actor });
    const from = m.phase;
    m.phase = to;
    m.updated_at = at;
    // like the API (SDD §14.6): a cancelled migration takes no advisor decision
    if (to === 'cancelled') m.advisor_hold = null;
    if (to !== 'precopy' && to !== 'syncing' && to !== 'cutover') this.dropRunningPass(m);
    this.emit({ kind: 'migration.phase', plan_id: m.plan_id, migration_id: m.id, actor, message: `${m.vm.name}: ${from} → ${to}`, data: { from_phase: from, to_phase: to, reason, ...data } });
  }

  private sourceFor(plan: Plan): Provider {
    const provider = this.providers.find((p) => p.id === plan.source_provider_id);
    if (!provider) throw notFound('provider', plan.source_provider_id);
    return provider;
  }

  private destinationFor(plan: Plan): Provider {
    const provider = this.providers.find((p) => p.id === plan.destination_provider_id);
    if (!provider) throw notFound('provider', plan.destination_provider_id);
    return provider;
  }

  private evaluate(m: Migration, plan: Plan): void {
    const source = this.sourceFor(plan);
    const destination = this.destinationFor(plan);
    const dst = this.inventories[destination.id];
    const networks = dst && !Array.isArray(dst) ? dst.networks : {};
    const subnets = dst && !Array.isArray(dst) ? (dst.subnets ?? []) : [];
    const networkDetails = dst && !Array.isArray(dst) ? dst.network_details : undefined;
    // the plan's VMs as the API's validation reads them, its vm_ids from the source's inventory: never its migrations,
    // which a first validation has not all created yet and a VM taken out of the plan leaves behind (review RV25-01)
    const inventory = this.inventories[plan.source_provider_id];
    const selected = plan.vm_ids.flatMap((id) => (Array.isArray(inventory) ? inventory.filter((v) => v.source_id === id) : []));
    // the destination's token roles, as its check reported them, and its policy profile - a report that names none
    // reads the provider's declarations (SDD §9.3, §10, §21.5)
    const roles = capabilityRoles(destination.capabilities);
    const profile = policyProfileOf(destination.capabilities, destination);
    m.findings = preflight(m.vm, source.kind, plan, networks, { subnets, selected, networkDetails, roles, profile, destination: providerLabel(destination) });
    // what an earlier attempt left at the destination keeps a handover out (SDD §9.2, review 39 lead L1)
    const elig = eligibility(m.vm, source.kind, plan, source, destination, m.findings, keptAfterRollback(m));
    m.estimates = (Object.keys(elig) as Strategy[]).map((s) => estimate(m.vm, s, plan, elig[s] ?? []));
    m.strategy = this.select(m, plan);
    m.estimate = m.estimates.find((e) => e.strategy === m.strategy) ?? null;
    // what the rules would probe (SDD §9.6): inactive until a person adds it to the plan
    m.suggested_probes = suggestProbes(m.vm);
  }

  private select(m: Migration, plan: Plan): Strategy {
    const eligible = m.estimates.filter((e) => e.eligible);
    const wanted = plan.strategy_overrides[m.vm.source_id] ?? (plan.default_strategy !== 'auto' ? plan.default_strategy : null);
    if (wanted && eligible.some((e) => e.strategy === wanted)) return wanted;
    if (!eligible.length) return m.estimates[0]?.strategy ?? m.strategy;
    const pool = plan.selection_policy === 'simplest_meeting_slo' && eligible.some((e) => e.meets_slo) ? eligible.filter((e) => e.meets_slo) : eligible;
    const best = pool.reduce((a, b) => (b.downtime_s < a.downtime_s ? b : a));
    const ties = pool.filter((e) => e.downtime_s - best.downtime_s < Math.max(60, best.downtime_s * 0.1));
    return ties.sort((a, b) => SIMPLICITY[a.strategy] - SIMPLICITY[b.strategy])[0]?.strategy ?? best.strategy;
  }

  /** A fresh assessment or strategy needs a fresh approval (SDD §5.4): nothing given before carries over. */
  private clearApprovals(m: Migration): void {
    m.approvals = [];
    clearRequest(m);
  }

  private finishValidation(m: Migration, plan: Plan, actor: string): void {
    // the attempts so far, with the strategies they ran: evaluate() chooses anew (SDD §14.4, ADV-02)
    const earlier = m.attempts > 0 ? { id: m.id, attempts: m.attempts, attempt_strategies: [...m.attempt_strategies] } : null;
    this.evaluate(m, plan);
    const rec = this.decideStrategy(m, plan, actor, earlier);
    // like the API (SDD §5.4, §14.6): a waiting decision holds the migration from this write on, and the one stored is
    // what a decision acts on while no hold names one (review RV51-08)
    m.advisor_hold = rec?.decision.status === 'pending_approval' ? rec.id : null;
    if (rec) m.last_recommendation = rec.id;
    this.clearApprovals(m);
    // its VM's wave in the plan, as the API's assessment writes it (SDD §9.4, review RV39-03)
    m.wave_id = plan.waves.find((w) => w.vm_ids.includes(m.vm.source_id))?.id ?? null;
    const blocked = m.findings.some((f) => f.severity === 'blocker');
    this.transition(m, blocked ? 'blocked' : 'ready', blocked ? 'blocker findings present' : 'pre-flight passed', actor);
    if (rec) {
      // like the API (SDD §14.5): recommendation.created, then the first decision event
      this.recommendations.push(rec);
      for (const event of auditEventsOf(rec)) this.emit({ plan_id: plan.id, migration_id: m.id, ...event });
    }
  }

  /**
   * Like validate_plan (SDD §14.4): an automatic choice whose advisor scope is not empty goes to the
   * advisor, decide() takes the decision, and the migration runs its final option.
   */
  private decideStrategy(
    m: Migration,
    plan: Plan,
    actor: string,
    earlier: Pick<Migration, 'id' | 'attempts' | 'attempt_strategies'> | null,
  ): AdvisorRecommendation | null {
    const automatic = plan.default_strategy === 'auto' && !(m.vm.source_id in plan.strategy_overrides);
    const eligible = m.estimates.filter((e) => e.eligible).map((e) => e.strategy);
    const scope = advisorScope(m.estimates, plan.selection_policy);
    if (!automatic || !scope.length || eligible.length < 2) return null;
    const advice = this.advise(m.vm, scope, m.estimates);
    const policy = policyFor(this.policies, plan);
    // the facts measured against the recommended option, or the deterministic one (SDD §14.4, ADV-02)
    const evidenceOption = advice.option && eligible.includes(advice.option as Strategy) ? advice.option : m.strategy;
    const evidence = buildStrategyEvidence(m.vm, plan, this.sourceFor(plan).kind, m.estimates, m.findings, evidenceOption, earlier, m.id);
    const { status, fallback, reason } = decided(advice, scope, policy, evidenceCount(evidence));
    const query = `${evidenceOption} ${m.vm.os_type ?? 'unknown OS'} ${sizeBucket(m.vm.disk_bytes)}`;
    const at = new Date(this.now()).toISOString();
    const recorded = this.recommendations.filter((r) => r.migration_id === m.id).length;
    // the advisor's input, named by the SHA-256 of its canonical JSON and written once (SDD §14.5)
    // the destination by kind and series: its provider's name never leaves for Jev (SDD §1 naming, §13.4)
    const destination = this.destinationFor(plan);
    const snapshot = strategySnapshot({
      vm: m.vm, plan, estimates: m.estimates, findings: m.findings, scope, deterministic: m.strategy, at,
      destination: destinationDescriptor(destination), destinationName: providerLabel(destination),
    });
    if (!this.snapshots.has(snapshot.sha256)) this.snapshots.set(snapshot.sha256, snapshot);
    const rec = strategyRecommendation({
      id: `rec-${m.id.slice(4)}${(recorded + 1).toString(16).padStart(2, '0')}`,
      at,
      migration: m,
      providerId: plan.source_provider_id,
      option: advice.option,
      confidence: advice.confidence,
      options: eligible,
      status,
      deterministic: m.strategy,
      fallback,
      snapshotSha256: snapshot.sha256,
      policyId: policy.id,
      threshold: policy.threshold,
      rules: { require_distinct_approver: policy.require_distinct_approver, require_override_reason: policy.require_override_reason },
      requestedBy: actor,
      reasonCode: reason,
      evidenceOption,
      evidence,
      similarIncidents: this.similarIncidents(query, 3).hits.slice(0, 3),
    });
    // a decision that waits keeps the deterministic choice and holds the migration (SDD §14.6)
    const final = (rec.decision.final_option ?? m.strategy) as Strategy;
    m.strategy = final;
    m.estimate = m.estimates.find((e) => e.strategy === final) ?? null;
    return rec;
  }

  // ---------------------------------------------------------------------------------------
  // HTTP
  // ---------------------------------------------------------------------------------------

  principal(token: string | null): Me | null {
    if (!token) return this.health.auth_disabled ? { name: 'anonymous', role: 'admin' } : null;
    const user = this.users.get(token.trim());
    if (user) return user;
    const t = token.trim().toLowerCase();
    for (const role of ['admin', 'approver', 'operator', 'viewer'] as const) {
      if (t === role || t.startsWith(`${role}:`) || t.startsWith(`smg_${role}`)) return { name: PERSONAS[role], role };
    }
    return null;
  }

  handle(method: string, path: string, query: URLSearchParams, body: unknown, token: string | null): MockResponse {
    this.request = { method, path };
    try {
      return this.route(method, path, query, body, token);
    } catch (error) {
      if (error instanceof HttpError) return { status: error.status, body: { error: { code: error.code, message: error.message } } };
      // like the API (SDD §12): an unexpected error never exposes internals
      return { status: 500, body: { error: { code: 'internal_error', message: 'internal error' } } };
    }
  }

  /**
   * A route's role check, like the API's require_role (SDD §12, §13.1): the token "locked" simulates
   * the control plane's per-address lockout (SDD §15.1), which like the real one only ever answers a
   * route with a role check — the public health routes have none.
   */
  private require(token: string | null, min: Role, _path: string): Me {
    const me = this.principal(token);
    if (!me) {
      if (token?.trim().toLowerCase() === 'locked') {
        this.auditDenied('too many failed authentication attempts', min, null);
        throw new HttpError(429, 'too_many_requests', 'too many failed authentication attempts from this address; retry in a minute');
      }
      this.auditDenied('missing or invalid bearer token', min, null);
      throw new HttpError(401, 'unauthorized', 'missing or invalid bearer token');
    }
    if (!hasRole(me.role, min)) {
      this.auditDenied(`role ${me.role} is below ${min}`, min, me);
      throw new HttpError(403, 'forbidden', `this action requires the ${min} role`);
    }
    return me;
  }

  /** `auth.denied` in the API's shape (SDD §13.1); the in-browser mock's client is the browser. */
  private auditDenied(reason: string, required: Role, me: Me | null): void {
    const { method } = this.request;
    const path = `${DEFAULT_API_BASE}/${this.request.path.replace(/^\//, '')}`;
    this.emit({
      kind: 'auth.denied',
      plan_id: null,
      migration_id: null,
      actor: me?.name ?? 'unauthenticated',
      message: `${method} ${path}: ${reason}`,
      data: { path, method, reason, required_role: required, client: 'browser' },
    });
  }

  private plan(id: string): Plan {
    const plan = this.plans.find((p) => p.id === id);
    if (!plan) throw notFound('plan', id);
    return plan;
  }

  private migration(id: string): Migration {
    const migration = this.migrations.find((m) => m.id === id);
    if (!migration) throw notFound('migration', id);
    return migration;
  }

  private route(method: string, path: string, query: URLSearchParams, body: unknown, token: string | null): MockResponse {
    const seg = path.split('/').filter(Boolean);
    const [root, id, sub, subsub] = seg;
    const input = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;

    if (method === 'GET' && root === 'health') return ok(this.health);

    if (root === 'me' && method === 'GET') return ok(this.require(token, 'viewer', path));

    if (root === 'users' && !id) {
      const me = this.require(token, 'admin', path);
      const users = [...ROLES.map((role) => ({ name: PERSONAS[role], role })), ...this.users.values()];
      if (method === 'GET') return ok(users);
      if (method === 'POST') {
        if (this.health.auth_disabled) throw new HttpError(409, 'conflict', 'enable authentication before adding dashboard users');
        const name = typeof input.name === 'string' ? input.name.trim() : '';
        const role = input.role === undefined ? 'viewer' : input.role;
        if (!/^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/.test(name) || ['anonymous', 'unauthenticated', 'cli', 'system'].includes(name.toLowerCase()) || !ROLES.includes(role as Role) || Object.keys(input).some((key) => !['name', 'role'].includes(key))) {
          throw new HttpError(422, 'validation_error', 'Choose a valid user name and role.');
        }
        if (users.some((user) => user.name.toLowerCase() === name.toLowerCase())) throw new HttpError(409, 'conflict', 'a user with this name already exists');
        const user: Me = { name, role: role as Role };
        const issued = `smg_${crypto.randomUUID()}`;
        this.users.set(issued, user);
        this.emit({ kind: 'user.created', actor: me.name, message: `User ${name} created`, plan_id: null, migration_id: null, data: { name, role } });
        return ok({ ...user, token: issued }, 201);
      }
    }

    if (root === 'providers') {
      if (!id) {
        if (method === 'GET') {
          this.require(token, 'viewer', path);
          return ok(this.providers);
        }
        if (method === 'POST') return this.createProvider(this.require(token, 'admin', path), input);
      } else {
        const provider = this.providers.find((p) => p.id === id);
        if (!provider) {
          // like the API (SDD §12, §13.2): the route's role first, then the lookup
          const writes = (!sub && (method === 'PATCH' || method === 'DELETE')) || ((sub === 'credentials' || sub === 'conversion-key') && method === 'PUT');
          this.require(token, writes ? 'admin' : sub === 'check' && method === 'POST' ? 'operator' : 'viewer', path);
          // a PATCH's body alone is judged before the lookup, as the API's update_provider (review RV22-07)
          if (!sub && method === 'PATCH') this.refusePatchBody(input);
          // so is a PUT of credentials or a conversion key (review RV23-03)
          if (method === 'PUT' && (sub === 'credentials' || sub === 'conversion-key')) refuseCredentialBody(sub, input);
          throw notFound('provider', id);
        }
        if (!sub && method === 'GET') {
          this.require(token, 'viewer', path);
          return ok(provider);
        }
        if (!sub && method === 'PATCH') return this.patchProvider(this.require(token, 'admin', path), provider, input);
        if (sub === 'credentials' && method === 'PUT') return this.setCredentials(this.require(token, 'admin', path), provider, input);
        if (sub === 'conversion-key' && method === 'PUT') return this.setConversionKey(this.require(token, 'admin', path), provider, input);
        if (!sub && method === 'DELETE') {
          const me = this.require(token, 'admin', path);
          const users = this.plansThatNeed(id);
          if (users.length) {
            const rest = users.length - 10;
            throw new HttpError(
              409,
              'conflict',
              `provider is used by plan(s) that still need it: ${users.slice(0, 10).join(', ')}${rest > 0 ? ` and ${rest} more` : ''}; ` +
                'a plan needs its providers until it completes and its VMs are finalized',
            );
          }
          this.providers.splice(this.providers.indexOf(provider), 1);
          // its inventory goes with it: a provider created again under the id never reads it (review RV21-12)
          delete this.inventories[provider.id];
          this.emit({ kind: 'provider.deleted', plan_id: null, migration_id: null, actor: me.name, message: `provider ${provider.id} deleted`, data: { provider_id: provider.id } });
          return { status: 204 };
        }
        if (sub === 'check' && method === 'POST') {
          const me = this.require(token, 'operator', path);
          refuseEmptyBody(input);
          provider.last_checked_at = new Date(this.now()).toISOString();
          if (provider.status === 'unknown') {
            // a mock convention: an endpoint whose host starts with "unreachable" fails the test
            if (/^https?:\/\/unreachable[.-]/i.test(provider.endpoint)) {
              provider.status = 'error';
              provider.status_message = 'The endpoint did not answer (connection timed out).';
            } else {
              provider.status = 'ok';
              // as the API's check: a provider that answers has no status message (SDD §10, review RV22-03)
              provider.status_message = null;
              // the API's keys: a vCenter's VMs counted; an OpenStack cloud's token roles, maxima, pools and TLS, and a
              // destination's series, tier and policy profile by what it declares (SDD §10, §10.1)
              const inventory = this.inventories[provider.id];
              provider.capabilities = provider.kind === 'vmware' ? vmwareCheck(Array.isArray(inventory) ? inventory : []) : checkOf(provider);
            }
          }
          this.emit({ kind: 'provider.checked', plan_id: null, migration_id: null, actor: me.name, message: `provider ${provider.id}: ${provider.status}`, data: { provider_id: provider.id, status: provider.status, message: provider.status_message } });
          return ok(provider);
        }
        if (sub === 'inventory' && method === 'GET') {
          this.require(token, 'viewer', path);
          if (provider.status === 'error') throw new HttpError(502, 'provider_error', `${provider.id}: ${provider.status_message ?? 'unreachable'}`);
          // a destination's answer names its token's roles, as the API demo's (SDD §10, review RV20-10)
          return ok(this.inventories[provider.id] ?? (provider.role === 'source' ? [] : { networks: {}, flavors: [], volume_types: [], quotas: {}, projects: [], roles: [...DEMO_TOKEN_ROLES] }));
        }
      }
    }

    if (root === 'plans') {
      const res = this.routePlans(method, path, query, input, token, id, sub, subsub);
      if (res) return this.withDerivedStatus(res);
    }

    if (root === 'demo' && id === 'vms' && sub && subsub === 'power' && method === 'POST') {
      this.require(token, 'operator', path);
      return this.demoPower(sub, input);
    }

    if (root === 'migrations') {
      if (!id && method === 'GET') {
        this.require(token, 'viewer', path);
        const rows = this.migrations.filter(
          (m) =>
            (!query.get('plan_id') || m.plan_id === query.get('plan_id')) &&
            (!query.get('phase') || m.phase === query.get('phase')) &&
            (!query.get('wave_id') || m.wave_id === query.get('wave_id')),
        );
        // the API's bounds (SDD §12): limit 1…5000, default all; offset from 0
        const limit = intParam(query, 'limit', rows.length, 1, 5000);
        const offset = intParam(query, 'offset', 0, 0);
        // each as its summary, as the API builds it (SDD §4, §12)
        const view = query.get('view') ?? 'full';
        if (view !== 'full' && view !== 'summary') throw new HttpError(422, 'validation_error', "query.view: Input should be 'full' or 'summary'");
        const page = rows.slice(offset, offset + limit);
        return ok(view === 'summary' ? page.map(summaryOf) : page);
      }
      if (id && !sub && method === 'GET') {
        this.require(token, 'viewer', path);
        return ok(this.migration(id));
      }
      if (id && sub === 'strategy' && method === 'PUT') {
        const me = this.require(token, 'operator', path);
        return this.setStrategy(me, this.migration(id), input);
      }
      if (id && sub && method === 'POST') {
        const min: Role = sub === 'approve' || sub === 'cutover' || sub === 'force-cutover' || sub === 'finalize' ? 'approver' : 'operator';
        const me = this.require(token, min, path);
        return this.migrationAction(me, this.migration(id), sub, input);
      }
    }

    if (root === 'events' && method === 'GET' && !id) {
      this.require(token, 'viewer', path);
      const since = Number(query.get('since') ?? 0) || 0;
      // before: only events below that seq, so tail + before is the page before the oldest shown (SDD §12)
      const before = query.has('before') ? Number(query.get('before')) : Infinity;
      if (!(before >= 1)) throw new HttpError(422, 'validation_error', 'before: Input should be greater than or equal to 1.');
      const limit = intParam(query, 'limit', 500, 1, 1000); // the API's bounds, never clamped (SDD §12)
      const planId = query.get('plan_id');
      const migrationId = query.get('migration_id');
      // audit: only the audit events (SDD §14.5)
      const audit = query.get('audit') === 'true';
      const matching = this.events.filter(
        (e) =>
          e.seq > since &&
          e.seq < before &&
          (!planId || e.plan_id === planId) &&
          (!migrationId || e.migration_id === migrationId) &&
          (!audit || (AUDIT_EVENT_KINDS as readonly string[]).includes(e.kind)),
      );
      // tail: the newest `limit` matching events instead of the first, still ascending (SDD §12)
      return ok(query.get('tail') === 'true' ? matching.slice(-limit) : matching.slice(0, limit));
    }

    if (root === 'stats' && method === 'GET') {
      this.require(token, 'viewer', path);
      return ok(this.stats(query.get('plan_id')));
    }

    if (root === 'advisor') {
      if (id === 'status' && method === 'GET') {
        this.require(token, 'viewer', path);
        const status: AdvisorStatus = {
          // like a compose stack: the jev sidecar forwards to the remote TypeSafe API (SDD §13.4)
          jev: { mode: 'http', available: true, last_error: null, deployment: 'remote', endpoint_host: null, console_sent: false },
          // like the API (SDD §14.3): an answered call clears the last error
          memory: { enabled: true, available: true, last_error: null },
        };
        return ok(status);
      }
      if (id === 'data-sent' && method === 'GET') {
        this.require(token, 'viewer', path);
        return ok({ items: DATA_SENT });
      }
      if (id === 'policies') {
        if (!sub && method === 'GET') {
          this.require(token, 'viewer', path);
          const builtin: AdvisorPolicy = { ...GLOBAL_DEFAULT, name: 'Global default', scope: { provider_ids: [], plan_ids: [] }, decision_types: [], min_evidence_for_auto_apply: 0, created_at: '1970-01-01T00:00:00Z', updated_at: '1970-01-01T00:00:00Z', updated_by: 'system' };
          return ok({ items: this.policies, default: builtin });
        }
        if (!sub && method === 'POST') return this.createPolicy(this.require(token, 'admin', path), input);
        if (sub && method === 'PUT') return this.replacePolicy(this.require(token, 'admin', path), sub, input);
        if (sub && method === 'DELETE') return this.deletePolicy(this.require(token, 'admin', path), sub);
      }
      if (id === 'recommendations' && sub && (subsub === 'approve' || subsub === 'override') && method === 'POST') {
        const me = this.require(token, 'admin', path);
        if (subsub === 'approve') refuseEmptyBody(input);
        return this.decideRecommendation(me, sub, subsub, input);
      }
      if (id === 'snapshots' && sub !== undefined && method === 'GET') {
        this.require(token, 'viewer', path);
        if (!/^[0-9a-f]{64}$/.test(sub)) throw new HttpError(422, 'validation_error', "sha256: String should match pattern '^[0-9a-f]{64}$'");
        const snapshot = this.snapshots.get(sub);
        if (!snapshot) throw notFound('advisor_snapshot', sub);
        return ok(snapshot);
      }
      if (id === 'recommendations' && method === 'GET') {
        this.require(token, 'viewer', path);
        return ok(sub === undefined ? this.listRecommendations(query) : this.recommendation(sub));
      }
      if (id === 'notes' && sub === undefined && method === 'GET') {
        this.require(token, 'viewer', path);
        return ok(this.listAdvisorNotes(query));
      }
      if (id === 'similar-incidents' && method === 'POST') {
        this.require(token, 'operator', path);
        // like the API (SDD §12): a query of 1-2000 characters, 1-20 results
        const queryText = typeof input.query === 'string' ? input.query : '';
        const limit = input.limit === undefined ? 5 : Number(input.limit);
        if (!queryText || queryText.length > ADVISOR_QUERY_MAX) throw new HttpError(422, 'validation_error', `query must be 1-${ADVISOR_QUERY_MAX} characters.`);
        if (!Number.isInteger(limit) || limit < 1 || limit > ADVISOR_LIMIT_MAX) throw new HttpError(422, 'validation_error', `limit must be 1-${ADVISOR_LIMIT_MAX}.`);
        return ok(this.similarIncidents(queryText, limit));
      }
    }

    if (root === 'metrics' && method === 'GET') {
      this.require(token, 'viewer', path);
      const lines = PHASES.map((p) => `seamless_migrations{phase="${p}"} ${this.migrations.filter((m) => m.phase === p).length}`);
      return { status: 200, text: `${lines.join('\n')}\n` };
    }

    throw new HttpError(404, 'not_found', `No mock route for ${method} /api/v1${path}.`);
  }

  // ---------------------------------------------------------------------------------------
  // Route handlers
  // ---------------------------------------------------------------------------------------

  /** A policy as the API accepts it (SDD §12, §14.6): 422 otherwise. */
  private policySpec(input: Record<string, unknown>): AdvisorPolicySpec {
    const scope = (typeof input.scope === 'object' && input.scope !== null ? input.scope : {}) as Record<string, unknown>;
    const list = (value: unknown) => (Array.isArray(value) ? value.map(String) : []);
    const spec: AdvisorPolicySpec = {
      name: typeof input.name === 'string' ? input.name : '',
      scope: { provider_ids: list(scope.provider_ids), plan_ids: list(scope.plan_ids) },
      decision_types: list(input.decision_types) as AdvisorPolicySpec['decision_types'],
      mode: input.mode as AdvisorPolicySpec['mode'],
      threshold: Number(input.threshold),
      // a new policy asks for 3 facts unless told otherwise, like the API (SDD §4.2, ADV-02)
      min_evidence_for_auto_apply: input.min_evidence_for_auto_apply === undefined ? 3 : Number(input.min_evidence_for_auto_apply),
      require_override_reason: input.require_override_reason === undefined ? true : Boolean(input.require_override_reason),
      require_distinct_approver: input.require_distinct_approver === undefined ? true : Boolean(input.require_distinct_approver),
    };
    const problems: string[] = [];
    if (!spec.name.trim() || spec.name.length > 200) problems.push('name: 1-200 characters');
    if (!POLICY_MODES.includes(spec.mode)) problems.push(`mode: one of ${POLICY_MODES.join(', ')}`);
    if (!Number.isFinite(spec.threshold) || spec.threshold < 0 || spec.threshold > 1) problems.push('threshold: 0 to 1');
    if (!Number.isInteger(spec.min_evidence_for_auto_apply) || spec.min_evidence_for_auto_apply < 0 || spec.min_evidence_for_auto_apply > 100) problems.push('min_evidence_for_auto_apply: 0 to 100');
    if (spec.decision_types.some((t) => !DECISION_TYPES.includes(t))) problems.push('decision_types: unknown type');
    if (spec.scope.provider_ids.length && spec.scope.plan_ids.length) problems.push('a policy scopes either providers or plans, not both');
    const unknown = [...spec.scope.provider_ids.filter((id) => !this.providers.some((p) => p.id === id)), ...spec.scope.plan_ids.filter((id) => !this.plans.some((p) => p.id === id))];
    if (unknown.length) problems.push(`unknown provider or plan: ${unknown.join(', ')}`);
    if (problems.length) throw new HttpError(422, 'validation_error', problems.join('; '));
    return spec;
  }

  private auditPolicy(me: Me, kind: string, id: string, before: AdvisorPolicy | null, after: AdvisorPolicy | null): void {
    this.emit({ kind, plan_id: null, migration_id: null, actor: `user:${me.name}`, message: `advisor policy ${id} ${kind.replace('policy.', '')}`, data: { target_id: id, before, after } });
  }

  private createPolicy(me: Me, input: Record<string, unknown>): MockResponse {
    const at = new Date(this.now()).toISOString();
    const policy: AdvisorPolicy = { ...this.policySpec(input), id: `pol-${hex(this.rand, 12)}`, created_at: at, updated_at: at, updated_by: me.name };
    this.policies.push(policy);
    this.auditPolicy(me, 'policy.created', policy.id, null, policy);
    return ok(policy, 201);
  }

  private replacePolicy(me: Me, id: string, input: Record<string, unknown>): MockResponse {
    const index = this.policies.findIndex((p) => p.id === id);
    if (index < 0) throw notFound('advisor_policy', id);
    const before = this.policies[index]!;
    const after: AdvisorPolicy = { ...this.policySpec(input), id, created_at: before.created_at, updated_at: new Date(this.now()).toISOString(), updated_by: me.name };
    this.policies[index] = after;
    this.auditPolicy(me, 'policy.updated', id, before, after);
    return ok(after);
  }

  private deletePolicy(me: Me, id: string): MockResponse {
    const index = this.policies.findIndex((p) => p.id === id);
    if (index < 0) throw notFound('advisor_policy', id);
    const [before] = this.policies.splice(index, 1);
    this.auditPolicy(me, 'policy.deleted', id, before!, null);
    return { status: 204 };
  }

  /** Approve or override an advisor decision, like the API (SDD §14.6). */
  private decideRecommendation(me: Me, recId: string, action: 'approve' | 'override', input: Record<string, unknown>): MockResponse {
    // like the API's body model (SDD §12, reviews RV42B-01, RV43B-07): an override's body is judged before anything
    if (action === 'override') refuseOverrideBody(input);
    if (recId.startsWith('rec-legacy-')) throw new HttpError(409, 'conflict', 'a legacy entry is history: it cannot be approved or overridden');
    const index = this.recommendations.findIndex((r) => r.id === recId);
    if (index < 0) throw notFound('recommendation', recId);
    const rec = this.recommendations[index]!;
    const m = this.migrations.find((x) => x.id === rec.migration_id);
    if (!m) throw new HttpError(409, 'conflict', `recommendation ${recId} decides nothing a person can change yet`);
    // like the API (SDD §14.6, reviews RV49-04, RV50-09, RV51-08): the hold names the one to decide, whatever its time;
    // without a hold, the one the last validation stored, else the newest
    const newest =
      m.advisor_hold ??
      m.last_recommendation ??
      this.recommendations.filter((r) => r.migration_id === m.id).reduce((a, b) => (b.created_at > a.created_at || (b.created_at === a.created_at && b.id > a.id) ? b : a)).id;
    if (newest !== recId) throw new HttpError(409, 'conflict', `a newer recommendation (${newest}) replaced ${recId}`);
    const plan = this.plan(m.plan_id);
    const policy = policyFor(this.policies, plan);
    // like the API (SDD §14.6): the rules of the policy that took the decision, as they were then; the policy in
    // force only for a decision that recorded none
    const distinct = rec.decision.require_distinct_approver ?? policy.require_distinct_approver;
    const reasonRequired = rec.decision.require_override_reason ?? policy.require_override_reason;
    // the policy whose rules the decision follows names it, as the API's does (SDD §14.6, audit AD-06)
    const recordedRules = rec.decision.require_distinct_approver !== null || rec.decision.require_override_reason !== null;
    const ruling = (recordedRules ? rec.decision.policy_id : null) ?? policy.id;
    // maker-checker (SDD §14.6, ADV-17): the validating user does not decide what their validation asked
    if (distinct && rec.requested_by === me.name) throw new HttpError(409, 'conflict', OWN_DECISION);
    // stripped and counted as the API's Python does: code points (SDD §14.6, audit AD-10)
    const reason = typeof input.reason === 'string' && pyStrip(input.reason) ? pyStrip(input.reason) : null;
    if (reason && pyLen(reason) > ACTION_TEXT_MAX) throw new HttpError(422, 'validation_error', `reason: at most ${ACTION_TEXT_MAX} characters.`);
    if (action === 'override' && reasonRequired && pyLen(reason ?? '') < OVERRIDE_REASON_MIN) {
      throw new HttpError(422, 'validation_error', `policy ${ruling} requires an override reason of at least ${OVERRIDE_REASON_MIN} characters`);
    }
    if (!['pending', 'blocked', 'ready'].includes(m.phase)) throw new HttpError(409, 'conflict', `the strategy cannot change in ${m.phase}`);
    // like the API (SDD §14.6): the plan's own choice stands over the newest recommendation, and an approved
    // decision is not approved again (one event per decision status change)
    // the plan's default strategy too: a validation honours it as an override (audit AD-04)
    const pinned = plan.strategy_overrides[m.vm.source_id] ?? (plan.default_strategy !== 'auto' ? plan.default_strategy : undefined);
    if (pinned) throw new HttpError(409, 'conflict', `The plan sets the strategy of ${m.vm.name} (${pinned}): change it in the plan.`);
    if (action === 'approve' && rec.decision.status === 'approved') throw new HttpError(409, 'conflict', 'The decision is already approved.');
    // an answer that could not be applied stays unapplied; an override is a person's call (audit AD-03)
    if (action === 'approve' && rec.recommendation.unusable) throw new HttpError(409, 'conflict', `nothing to approve: ${rec.recommendation.unusable}`);
    const eligible = m.estimates.filter((e) => e.eligible).map((e) => e.strategy as string);
    const chosen = action === 'override' ? input.option : rec.recommendation.option;
    const stored = m.estimates.find((e) => e.strategy === 'storage_handover');
    // a Retry or a validation saw the volumes held, or they are now (SDD §14.6, §9.2)
    if (chosen === 'storage_handover' && stored && (stored.reasons.includes(HELD_VOLUMES_REASON) || (stored.eligible && keptAfterRollback(m)))) {
      // like the API (SDD §14.6, §9.2, review RV41B-01): judged now, as PUT …/strategy - a Retry re-assesses no stored
      // estimate, and a handover would leave an earlier attempt's volumes behind
      const refusal = `storage_handover is not eligible for ${m.vm.name}: ${HELD_VOLUMES_REASON}.`;
      if (action === 'override') throw new HttpError(422, 'validation_error', refusal);
      throw new HttpError(409, 'conflict', `nothing to approve: ${refusal}`);
    }
    const at = new Date(this.now()).toISOString();
    let decided: AdvisorRecommendation;
    if (action === 'approve') {
      const recommended = rec.recommendation.option;
      if (recommended === null || !eligible.includes(recommended)) {
        throw new HttpError(409, 'conflict', `nothing to approve: ${recommended ?? 'no option'} is not an eligible option`);
      }
      decided = humanDecision(rec, { kind: 'approve', user: me.name }, at);
    } else {
      const option = String(input.option ?? '');
      if (!eligible.includes(option)) throw new HttpError(422, 'validation_error', `${option} is not an eligible option`);
      decided = humanDecision(rec, { kind: 'override', user: me.name, option, reason }, at);
    }
    decided.decision.policy_id = ruling;
    // the rules go on with a person's decision: a later one follows them too (SDD §14.6)
    decided.decision.require_distinct_approver = distinct;
    decided.decision.require_override_reason = reasonRequired;
    this.recommendations[index] = decided;
    const final = decided.decision.final_option as Strategy;
    const changed = final !== m.strategy;
    m.strategy = final;
    m.estimate = m.estimates.find((e) => e.strategy === final) ?? null;
    m.advisor_hold = null;
    if (changed) this.clearApprovals(m);
    m.updated_at = at;
    if (action === 'override') {
      plan.strategy_overrides = { ...plan.strategy_overrides, [m.vm.source_id]: final };
      plan.updated_at = at;
      this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `${plan.name}: strategy override for ${m.vm.name}`, data: { strategy_overrides: { [m.vm.source_id]: final } } });
    }
    this.emit({ plan_id: plan.id, migration_id: m.id, ...decisionEventOf(decided, rec.decision) });
    if (changed) this.emit({ kind: 'migration.action', plan_id: plan.id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: strategy set to ${final} by the advisor decision`, data: { action: 'strategy', strategy: final } });
    return ok(decided);
  }

  /** Stored and legacy recommendations, newest first (SDD §12, §14.4). */
  private allRecommendations(): AdvisorRecommendation[] {
    const items = [...this.recommendations, ...this.migrations.flatMap(legacyRecommendations)];
    return items.sort((a, b) => (a.created_at === b.created_at ? (a.id < b.id ? 1 : -1) : a.created_at < b.created_at ? 1 : -1));
  }

  private listRecommendations(query: URLSearchParams): RecommendationPage {
    const limit = intParam(query, 'limit', RECOMMENDATION_LIMIT_DEFAULT, 1, RECOMMENDATION_LIMIT_MAX);
    const offset = intParam(query, 'offset', 0, 0);
    const type = query.get('decision_type');
    if (type !== null && !DECISION_TYPES.includes(type as DecisionType)) {
      throw new HttpError(422, 'validation_error', `decision_type: Input should be ${DECISION_TYPES.map((t) => `'${t}'`).join(', ')}.`);
    }
    const migrationId = query.get('migration_id');
    const planId = query.get('plan_id');
    const items = this.allRecommendations().filter(
      (r) => (migrationId === null || r.migration_id === migrationId) && (planId === null || r.plan_id === planId) && (type === null || r.decision_type === type),
    );
    return { items: items.slice(offset, offset + limit), total: items.length, offset, limit };
  }

  /**
   * Every migration's advisor notes but strategy notes - those read as legacy recommendations - newest first, like the
   * API (SDD §12): a stable sort keeps notes of the same time in the migrations' creation order and each one's note order.
   */
  private listAdvisorNotes(query: URLSearchParams): AdvisorNoteList {
    const limit = intParam(query, 'limit', ADVISOR_NOTES_LIMIT_DEFAULT, 1, ADVISOR_NOTES_LIMIT_MAX);
    const items: AdvisorNoteEntry[] = this.migrations.flatMap((m) =>
      m.advisor_notes.filter((n) => n.kind !== 'strategy').map((n) => ({ ...n, migration_id: m.id, plan_id: m.plan_id, vm_name: m.vm.name })),
    );
    items.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
    return { items: items.slice(0, limit) };
  }

  private recommendation(id: string): AdvisorRecommendation {
    const found = this.allRecommendations().find((r) => r.id === id);
    if (!found) throw notFound('recommendation', id);
    return found;
  }

  private createProvider(me: Me, input: Record<string, unknown>): MockResponse {
    const id = String(input.id ?? '');
    if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(id)) throw new HttpError(422, 'validation_error', 'id: must match ^[a-z0-9][a-z0-9-]{1,62}$.');
    // a name missing or not a string is refused in pydantic's words, as the API's model refuses it: stored, every text
    // that names the provider would read it (SDD §4.2, review RV21-07)
    if (typeof input.name !== 'string') throw new HttpError(422, 'validation_error', input.name === undefined ? 'name: Field required' : 'name: Input should be a valid string');
    if (isBlank(input.name)) throw new HttpError(422, 'validation_error', 'name: must not be blank');
    if (this.providers.some((p) => p.id === id)) throw new HttpError(409, 'conflict', `provider '${id}' already exists`);
    // like the API: provider-{id} and provider-{id}-ssh must stay apart from every other provider's (SDD §13.3)
    const clash = this.providers.find((p) => id === `${p.id}-ssh` || p.id === `${id}-ssh`);
    if (clash) {
      throw new HttpError(409, 'conflict', `provider id '${id}' would share a secret with provider '${clash.id}': Seamless stores a provider's secrets as provider-<id> and provider-<id>-ssh`);
    }
    checkDistribution(input);
    checkDeclarations(input);
    checkSecretNames(input, new Set(['credentials_secret', 'conversion_host']), id);
    const defaults: Partial<Provider> = {
      cloud: null,
      credentials_secret: null,
      region: null,
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: null,
      distribution: null,
      release: null,
      policy_profile: null,
    };
    const provider = {
      ...defaults,
      ...(input as unknown as Provider),
      capabilities: {},
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      credentials_updated_at: null,
      conversion_key_updated_at: null,
    } as Provider;
    this.providers.push(provider);
    this.emit({ kind: 'provider.created', plan_id: null, migration_id: null, actor: me.name, message: `provider ${provider.id} created`, data: { provider_id: provider.id, kind: provider.kind, role: provider.role, ...secretNames(provider) } });
    return ok(provider, 201);
  }

  /** PATCH /providers/{id}: editable fields only, 409 while a running/paused plan uses it. */
  /** What PATCH /providers/{id} refuses from its body alone, before it looks the provider up, as the API (SDD §4.2, §12). */
  private refusePatchBody(input: Record<string, unknown>): void {
    const unknown = Object.keys(input).filter((k) => !PROVIDER_EDITABLE.has(k)).sort();
    if (unknown.length) throw new HttpError(422, 'validation_error', `fields cannot be changed: ${unknown.join(', ')}`);
    // a blank name is refused, as the API refuses it (SDD §4.2, review RV20-11)
    if (typeof input.name === 'string' && isBlank(input.name)) throw new HttpError(422, 'validation_error', 'name: must not be blank');
  }

  private patchProvider(me: Me, provider: Provider, input: Record<string, unknown>): MockResponse {
    this.refusePatchBody(input);
    const users = this.plans.filter(
      (p) => ['running', 'paused'].includes(p.status) && (p.source_provider_id === provider.id || p.destination_provider_id === provider.id),
    );
    if (users.length) throw new HttpError(409, 'conflict', `provider is used by running or paused plan(s): ${users.map((p) => p.id).join(', ')}`);
    // the API validates the merged provider after its 409: a name that is not a string answers 422 (review RV21-07)
    if ('name' in input && typeof input.name !== 'string') throw new HttpError(422, 'validation_error', 'name: Input should be a valid string');
    checkDistribution({ ...provider, ...input });
    checkDeclarations({ ...provider, ...input });
    checkSecretNames(input, new Set(Object.keys(input)), provider.id);
    Object.assign(provider, input, { status: 'unknown', status_message: null, last_checked_at: null, capabilities: {} });
    this.emit({ kind: 'provider.updated', plan_id: null, migration_id: null, actor: me.name, message: `provider ${provider.id} updated`, data: { provider_id: provider.id, fields: Object.keys(input).sort(), ...secretNames(provider) } });
    return ok(provider);
  }

  /** PUT /providers/{id}/credentials: the mock keeps no values, only the "stored" stamp. */
  private setCredentials(me: Me, provider: Provider, input: Record<string, unknown>): MockResponse {
    refuseCredentialBody('credentials', input);
    // blank as the API's Python strip judges it (review RV24-06)
    const values = Object.fromEntries(Object.entries(input).filter(([, v]) => typeof v === 'string' && pyStrip(v))) as Record<string, string>;
    const vmware = provider.kind === 'vmware';
    const allowed = vmware ? ['username', 'password', 'datacenter'] : ['auth_url', 'username', 'password', 'project_name', 'user_domain_name', 'project_domain_name', 'application_credential_id', 'application_credential_secret', 'interface'];
    const extra = Object.keys(values).filter((k) => !allowed.includes(k)).sort();
    if (extra.length) throw new HttpError(422, 'validation_error', `not used by a ${provider.kind} provider: ${extra.join(', ')}`);
    const app = !vmware && Boolean(values.application_credential_id || values.application_credential_secret);
    const required = vmware ? ['username', 'password'] : app ? ['application_credential_id', 'application_credential_secret'] : ['username', 'password', 'project_name'];
    const missing = required.filter((k) => !values[k]);
    if (missing.length) throw new HttpError(422, 'validation_error', `missing: ${missing.join(', ')}`);
    Object.assign(provider, {
      credentials_secret: `provider-${provider.id}`,
      credentials_updated_at: new Date(this.now()).toISOString(),
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      capabilities: {},
    });
    this.emit({ kind: 'provider.credentials_updated', plan_id: null, migration_id: null, actor: me.name, message: `credentials of provider ${provider.id} updated`, data: { provider_id: provider.id, secret: provider.credentials_secret, keys: Object.keys(values).sort() } });
    return ok(provider);
  }

  private setConversionKey(me: Me, provider: Provider, input: Record<string, unknown>): MockResponse {
    refuseCredentialBody('conversion-key', input);
    const key = pyStrip(String(input.private_key ?? ''));
    if (!key.startsWith('-----BEGIN') || !key.includes('PRIVATE KEY-----')) throw new HttpError(422, 'validation_error', 'private_key must be an OpenSSH or PEM private key');
    const host = provider.conversion_host ?? { manage: false, name: null, flavor: null, external_network: null, image: null, ssh_user: 'cloud-user', address: null, ssh_allowed_cidr: null, ssh_key_secret: null };
    const secret = `provider-${provider.id}-ssh`;
    Object.assign(provider, {
      conversion_host: { ...host, ssh_key_secret: secret },
      conversion_key_updated_at: new Date(this.now()).toISOString(),
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      capabilities: {},
    });
    this.emit({ kind: 'provider.credentials_updated', plan_id: null, migration_id: null, actor: me.name, message: `conversion-host key of provider ${provider.id} updated`, data: { provider_id: provider.id, secret, keys: ['private_key'] } });
    return ok(provider);
  }

  /**
   * The approval policy is approver-only (SDD §12): an operator may send a policy field only at
   * its default (POST) or at the plan's current value (PATCH), and the refusal precedes every
   * other check, like the API (QASuite S-01).
   */
  private refusePolicyChange(me: Me, input: Record<string, unknown>, current: Record<string, unknown>): void {
    if (hasRole(me.role, 'approver')) return;
    const touched = POLICY_FIELDS.filter((key) => key in input && stable(input[key] ?? null) !== stable(current[key] ?? null));
    if (!touched.length) return;
    const reason = `setting ${[...touched].sort().join(', ')} requires the approver role`;
    this.auditDenied(reason, 'approver', me);
    throw new HttpError(403, 'forbidden', reason);
  }

  /** Plan name and description bounds, like the API's create and patch (SDD §12). */
  private refuseLongPlanTexts(input: Record<string, unknown>): void {
    const problems: string[] = [];
    if (typeof input.name === 'string' && input.name.length > PLAN_NAME_MAX) problems.push(`name: at most ${PLAN_NAME_MAX} characters (got ${input.name.length})`);
    if (typeof input.description === 'string' && input.description.length > PLAN_DESCRIPTION_MAX) {
      problems.push(`description: at most ${PLAN_DESCRIPTION_MAX} characters (got ${input.description.length})`);
    }
    if (problems.length) throw new HttpError(422, 'validation_error', problems.join('; '));
  }

  private routePlans(
    method: string,
    path: string,
    query: URLSearchParams,
    input: Record<string, unknown>,
    token: string | null,
    id: string | undefined,
    sub: string | undefined,
    subsub: string | undefined,
  ): MockResponse | null {
    if (!id) {
      if (method === 'GET') {
        this.require(token, 'viewer', path);
        // SDD §12: status filter and limit/offset paging, like the migrations list
        const rows = this.plans.filter((p) => !query.get('status') || p.status === query.get('status'));
        // the API's bounds (SDD §12): limit 1…1000, default all; offset from 0
        const limit = intParam(query, 'limit', rows.length, 1, 1000);
        const offset = intParam(query, 'offset', 0, 0);
        return ok(rows.slice(offset, offset + limit));
      }
      if (method === 'POST') return this.createPlan(this.require(token, 'operator', path), input);
      return null;
    }
    if (!sub && method === 'GET') {
      this.require(token, 'viewer', path);
      return ok(this.plan(id));
    }
    if (!sub && method === 'PATCH') return this.patchPlan(this.require(token, 'operator', path), this.plan(id), input);
    if (sub === 'waves' && subsub === 'auto' && method === 'POST') return this.autoWaves(this.require(token, 'operator', path), this.plan(id), input);
    if ((sub === 'validate' || sub === 'start' || sub === 'pause') && method === 'POST') {
      const me = this.require(token, 'operator', path);
      refuseEmptyBody(input);
      if (sub === 'validate') return this.validatePlan(me, this.plan(id));
      return sub === 'start' ? this.startPlan(me, this.plan(id)) : this.pausePlan(me, this.plan(id));
    }
    if (sub === 'cancel' && method === 'POST') return this.cancelPlan(this.require(token, 'operator', path), this.plan(id), input);
    if (sub === 'finalize' && method === 'POST') return this.finalizePlan(this.require(token, 'approver', path), this.plan(id), input);
    return null;
  }

  /** Every plan the API returns carries its derived status (SDD §8.1, ADV-19) and its migrations counted by phase (§4). */
  private withDerivedStatus(res: MockResponse): MockResponse {
    const out = (plan: Plan): Plan => {
      const counts: Plan['migration_counts'] = {};
      for (const m of this.migrations) if (m.plan_id === plan.id) counts[m.phase] = (counts[m.phase] ?? 0) + 1;
      return { ...plan, derived_status: derivePlanStatus(plan, this.migrations), migration_counts: counts };
    };
    const isPlan = (value: unknown): value is Plan => typeof value === 'object' && value !== null && 'vm_ids' in value && 'waves' in value;
    if (Array.isArray(res.body)) return { ...res, body: res.body.map((item: unknown) => (isPlan(item) ? out(item) : item)) };
    return isPlan(res.body) ? { ...res, body: out(res.body) } : res;
  }

  /** POST /plans/{id}/finalize (SDD §8.1): every completed migration of a wave or the plan, one approval. */
  private finalizePlan(me: Me, plan: Plan, input: Record<string, unknown>): MockResponse {
    const waveId = typeof input.wave_id === 'string' ? input.wave_id : null;
    const wave = waveId ? plan.waves.find((w) => w.id === waveId) : null;
    if (waveId && !wave) throw notFound('wave', waveId);
    const name = wave ? wave.name : plan.name;
    if (input.confirm !== name) throw new HttpError(400, 'bad_request', `confirm must equal the name '${name}'`);
    const scope = wave ? new Set(wave.vm_ids) : null;
    const waiting = this.migrations
      .filter((m) => m.plan_id === plan.id && m.phase === 'completed' && (!scope || scope.has(m.vm.source_id)))
      .sort((a, b) => a.vm.name.localeCompare(b.vm.name));
    if (waiting.length === 0) throw new HttpError(409, 'conflict', `no completed migration to finalize in ${name}`);
    const finalized: Migration[] = [];
    const skipped: Array<{ migration_id: string; reason: string }> = [];
    for (const m of waiting) {
      // like the API (SDD §8.1): every VM whose source runs again, also one whose finalize began deleting it
      if (m.source_running_at) {
        skipped.push({ migration_id: m.id, reason: SOURCE_RUNS_AGAIN });
        continue;
      }
      // like the API: one VM's error skips that VM, never the others (SDD §8.1)
      try {
        this.migrationAction(me, m, 'finalize', { confirm: m.vm.name, delete_source: Boolean(input.delete_source) });
        finalized.push(m);
      } catch (e) {
        const reason = e instanceof HttpError ? e.message : `finalize failed: ${e instanceof Error ? e.message : String(e)}`;
        skipped.push({ migration_id: m.id, reason });
      }
    }
    const names = new Map(waiting.map((m) => [m.id, m.vm.name]));
    this.emit({
      kind: 'plan.finalized',
      plan_id: plan.id,
      migration_id: null,
      actor: me.name,
      message: `${name}: ${finalized.length} finalized by ${me.name}${skipped.length ? `, ${skipped.length} skipped` : ''}`,
      data: { wave_id: waveId, finalized: finalized.map((m) => m.vm.name), skipped: skipped.map((s) => ({ vm: names.get(s.migration_id), ...s })) },
    });
    return ok({ finalized: finalized.map((m) => m.id), skipped });
  }

  /** The power state of a source VM: what the demo set, else stopped once its cutover stopped it (SDD §8.1). */
  sourcePower(sourceId: string): 'running' | 'stopped' {
    const set = this.power.get(sourceId);
    if (set) return set;
    const m = this.migrations.find((x) => x.vm.source_id === sourceId && x.phase !== 'cancelled' && x.phase !== 'rolled_back');
    if (m && (STOPPED_SOURCE.has(m.phase) || (m.downtime_started_at && !m.downtime_ended_at))) return 'stopped';
    const vm = Object.values(this.inventories).flatMap((inv) => (Array.isArray(inv) ? inv : [])).find((v) => v.source_id === sourceId);
    return vm?.power_state === 'running' ? 'running' : 'stopped';
  }

  /** POST /demo/vms/{source_id}/power (demo mode only, SDD §8.1). */
  private demoPower(sourceId: string, input: Record<string, unknown>): MockResponse {
    if (!this.health.demo) throw new HttpError(404, 'not_found', 'no such route outside demo mode');
    if (input.state !== 'on' && input.state !== 'off') throw new HttpError(422, 'validation_error', "state: Input should be 'on' or 'off'");
    const vm = Object.values(this.inventories).flatMap((inv) => (Array.isArray(inv) ? inv : [])).find((v) => v.source_id === sourceId);
    if (!vm) throw notFound('source VM', sourceId);
    this.power.set(sourceId, input.state === 'on' ? 'running' : 'stopped');
    return ok({ ...vm, power_state: this.sourcePower(sourceId) });
  }

  /** The source watch (SDD §8.1): a completed migration's source VM seen running is flagged once, and
   * cleared once seen stopped again; each change is an event. */
  private watchSources(): void {
    for (const m of this.migrations) {
      if (m.phase !== 'completed') continue;
      const running = this.sourcePower(m.vm.source_id) === 'running';
      if (running === (m.source_running_at !== null)) continue;
      m.source_running_at = running ? new Date(this.now()).toISOString() : null;
      // the event names the plan's destination provider, as the API's (SDD §1 naming)
      const plan = this.plans.find((p) => p.id === m.plan_id);
      const destination = destinationName(this.providers, plan);
      this.emit({
        kind: running ? 'migration.source_running' : 'migration.source_stopped',
        plan_id: m.plan_id,
        migration_id: m.id,
        actor: 'orchestrator',
        message: running
          ? `${m.vm.name}: the source VM runs again - its data diverges from ${destination}; stop it, or roll back`
          : `${m.vm.name}: the source VM is stopped again`,
        data: { power_state: running ? 'running' : 'stopped' },
      });
    }
  }

  private createPlan(me: Me, raw: Record<string, unknown>): MockResponse {
    const input = withCanonicalNicOverrides(raw);
    this.refusePolicyChange(me, input, defaultPlanFields(this.now()));
    this.refuseLongPlanTexts(input);
    const body = input as unknown as PlanCreate;
    if (!body.name || !String(body.name).trim()) throw new HttpError(422, 'validation_error', 'name is required.');
    if (!body.source_provider_id || !body.destination_provider_id) throw new HttpError(422, 'validation_error', 'source_provider_id and destination_provider_id are required.');
    // like the API (SDD §12): vm_ids is required; an empty list is a plan to fill later (the form asks for a VM)
    if (!Array.isArray(body.vm_ids)) throw new HttpError(422, 'validation_error', 'vm_ids: Field required');
    refusePlanSpec({ ...defaultPlanFields(this.now()), ...input });
    const source = this.providers.find((p) => p.id === body.source_provider_id && p.role === 'source');
    const destination = this.providers.find((p) => p.id === body.destination_provider_id && p.role === 'destination');
    if (!source || !destination) throw new HttpError(400, 'bad_request', 'Unknown source or destination provider.');
    const now = this.now();
    const plan: Plan = {
      ...defaultPlanFields(now),
      ...(body as Partial<Plan>),
      id: `plan-${hex(this.rand, 8)}`,
      name: String(body.name).trim(),
      description: body.description ?? null,
      waves: [],
      status: 'draft',
      created_at: new Date(now).toISOString(),
      updated_at: new Date(now).toISOString(),
    } as Plan;
    this.plans.unshift(plan);
    this.emit({ kind: 'plan.created', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" created with ${plan.vm_ids.length} VMs`, data: { status: 'draft' } });
    return ok(plan, 201);
  }

  /**
   * The plans that still need a provider, like the API (SDD §12), in creation order with why: one that has not
   * completed (a failed one can start again), or a completed one with a migration that holds its VM — it can be
   * rolled back until it is finalized, and its rollback needs both providers (§8.1) — or with a rolled-back one that
   * kept its copied data: its warm rollback kept the destination volumes for a retry, and a cancel cleans them up
   * with both providers (§5.1).
   */
  private plansThatNeed(providerId: string): string[] {
    const plans = this.plans
      .filter((p) => p.source_provider_id === providerId || p.destination_provider_id === providerId)
      .sort((a, b) => a.created_at.localeCompare(b.created_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    return plans.flatMap((p) => {
      // a cancelled plan's migrations are all cancelled, and the mock's cleanup after a cancel ends at once: none is
      // pending, as one keeps the API's providers (SDD §5.1, §12)
      if (p.status === 'cancelled') return [];
      if (p.status !== 'completed') return [`${p.id} ("${p.name}", ${p.status})`];
      const mine = this.migrations.filter((m) => m.plan_id === p.id);
      const n = mine.filter((m) => !RELEASES_VM.has(m.phase)).length;
      // the mock keeps no record: like the API without one, what the rollback kept (SDD §5.1, §9.3, §12, review RV39-02)
      const kept = mine.filter((m) => m.phase === 'rolled_back' && keptAfterRollback(m)).length;
      const why = [
        ...(n ? [`${n} VM${n === 1 ? '' : 's'} not finalized`] : []),
        ...(kept ? [`${kept} rolled-back VM${kept === 1 ? ' keeping its' : 's keeping their'} copied data`] : []),
      ];
      return why.length ? [`${p.id} ("${p.name}", ${why.join(', ')})`] : [];
    });
  }

  /** SDD §12: no plan edit or re-wave while a migration of the plan is in flight (409), each VM with what releases it. */
  private refuseInFlight(plan: Plan, before: string): void {
    const busy = this.migrations.filter((m) => m.plan_id === plan.id && blocksPlanChange(m));
    if (busy.length) throw new HttpError(409, 'conflict', inFlightRefusal(busy, before));
  }

  private patchPlan(me: Me, plan: Plan, raw: Record<string, unknown>): MockResponse {
    const input = withCanonicalNicOverrides(raw);
    // like the API (SDD §12): the body is a partial PlanCreate, a field it cannot change is refused first
    const unknown = Object.keys(input).filter((key) => !PLAN_EDITABLE_FIELDS.has(key)).sort();
    if (unknown.length) throw new HttpError(422, 'validation_error', `fields cannot be changed: ${unknown.join(', ')}`);
    this.refusePolicyChange(me, input, plan as unknown as Record<string, unknown>);
    this.refuseLongPlanTexts(input);
    if (!['draft', 'validated'].includes(plan.status)) throw new HttpError(409, 'conflict', `Plans can only be edited in draft or validated (this plan is ${plan.status}).`);
    this.refuseInFlight(plan, 'editing the plan');
    // like the API (SDD §12): a failed migration's rollback acts on the clouds its attempt used
    const providers = ['source_provider_id', 'destination_provider_id'] as const;
    if (providers.some((field) => field in input && input[field] !== plan[field])) {
      const failed = this.migrations.filter((m) => m.plan_id === plan.id && m.phase === 'failed').map((m) => m.vm.name);
      if (failed.length) {
        const noun = failed.length === 1 ? 'migration' : 'migrations';
        throw new HttpError(
          409,
          'conflict',
          `roll back or cancel the failed ${noun} (${failed.slice(0, 10).join(', ')}) before changing the plan's providers: its rollback acts on the clouds its attempt used`,
        );
      }
    }
    // like the API (SDD §12): the plan it would store is checked, kept fields included
    refusePlanSpec({ ...(plan as unknown as Record<string, unknown>), ...input });
    for (const key of Object.keys(input)) (plan as unknown as Record<string, unknown>)[key] = input[key];
    plan.status = 'draft';
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" updated`, data: { fields: Object.keys(input) } });
    return ok(plan);
  }

  /**
   * The plan's VMs as a validation and the wave planner read them (orchestrator._vms, SDD §5.4, review PL-09): from the
   * source each VM with no migration yet or one a validation re-assesses, any other migration's recorded vm; a VM the
   * source no longer reports is a provider error.
   */
  private planVms(plan: Plan): VMRef[] {
    const byVm = new Map(this.migrations.filter((m) => m.plan_id === plan.id).map((m) => [m.vm.source_id, m]));
    const inventory = (this.inventories[plan.source_provider_id] ?? []) as VMRef[];
    return plan.vm_ids.map((id) => {
      const m = byVm.get(id);
      if (m && !reassessed(m)) return m.vm;
      const vm = inventory.find((v) => v.source_id === id);
      if (!vm) throw new HttpError(502, 'provider_error', `VM '${id}': VM '${id}' not found`);
      return vm;
    });
  }

  private autoWaves(me: Me, plan: Plan, input: Record<string, unknown>): MockResponse {
    // like the API (SDD §12): the body is checked first, an integer from 1 to 1000, never clamped
    const size = input.max_wave_size ?? 10;
    if (typeof size !== 'number' || !Number.isInteger(size)) throw new HttpError(422, 'validation_error', 'max_wave_size: Input should be a valid integer');
    if (size < 1) throw new HttpError(422, 'validation_error', 'max_wave_size: Input should be greater than or equal to 1');
    if (size > 1000) throw new HttpError(422, 'validation_error', 'max_wave_size: Input should be less than or equal to 1000');
    // like the API (SDD §12): draft, validated or paused, and the plan returns to draft
    if (!['draft', 'validated', 'paused'].includes(plan.status)) throw new HttpError(409, 'conflict', `waves cannot change while the plan is ${plan.status}`);
    this.refuseInFlight(plan, 're-planning the waves');
    // the plan's VMs as the API's planner reads them (orchestrator._vms, SDD §9.4, review RV39-03): every VM of vm_ids,
    // a fresh plan's and one added since too - a migration a validation leaves alone as its recorded vm
    const vms = this.planVms(plan).sort((a, b) => a.disk_bytes - b.disk_bytes);
    const pilot = vms.slice(0, Math.min(3, vms.length));
    const rest = vms.slice(pilot.length);
    const waves: Wave[] = [];
    if (pilot.length) waves.push({ id: 'wave-1', name: 'Pilot', order: 1, vm_ids: pilot.map((vm) => vm.source_id), depends_on: [], max_parallel: 5 });
    for (let i = 0; i < rest.length; i += size) {
      const n = waves.length + 1;
      waves.push({ id: `wave-${n}`, name: `Wave ${n}`, order: n, vm_ids: rest.slice(i, i + size).map((vm) => vm.source_id), depends_on: [`wave-${n - 1}`], max_parallel: 5 });
    }
    // like the API: the migrations take their VM's wave at the next validation, which a plan back in draft needs before it
    // starts (SDD §9.4, review RV40-03)
    plan.waves = waves;
    plan.status = 'draft'; // like the API: new waves are validated again before a start (SDD §12)
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}": ${waves.length} waves planned`, data: { waves: waves.length } });
    return ok(plan);
  }

  /**
   * The VMs of `vmIds` that migrations of other plans with the same source hold (SDD §5.4), by source
   * id: `name (plan "…", plan-id, phase)` once per holding plan, like the API — plans validated before the rule may
   * hold a VM twice, and plan names need not be unique.
   */
  private heldElsewhere(plan: Plan, vmIds: readonly string[]): Map<string, string[]> {
    const others = new Map(this.plans.filter((p) => p.id !== plan.id && p.source_provider_id === plan.source_provider_id).map((p) => [p.id, p]));
    const held = new Map<string, Set<string>>();
    for (const m of this.migrations) {
      if (!others.has(m.plan_id) || !vmIds.includes(m.vm.source_id) || RELEASES_VM.has(m.phase)) continue;
      const holders = held.get(m.vm.source_id) ?? new Set<string>();
      holders.add(`${m.vm.name} (plan "${others.get(m.plan_id)?.name}", ${m.plan_id}, ${m.phase})`);
      held.set(m.vm.source_id, holders);
    }
    return new Map([...held].map(([id, holders]) => [id, [...holders].sort()]));
  }

  /** A new migration of `vm` in `plan`, created by validation as in the API (SDD §8). */
  private createMigration(plan: Plan, vm: VMRef, actor: string): Migration {
    const now = this.now();
    const source = this.sourceFor(plan);
    const created = new Date(now).toISOString();
    const m: Migration = {
      id: `mig-${hex(this.rand, 10)}`,
      plan_id: plan.id,
      wave_id: null,
      vm,
      strategy: source.kind === 'vmware' ? 'vmware_cold' : 'cold',
      phase: 'pending',
      phase_history: [{ from_phase: null, to_phase: 'pending', at: created, reason: 'migration created', actor }],
      progress_pct: 0,
      bytes_total: vm.used_bytes,
      bytes_transferred: 0,
      sync_passes: [],
      sync_bytes_dropped: 0,
      estimate: null,
      estimates: [],
      observed_scan_bps: null,
      resolved_mappings: { networks: {}, flavors: {}, volume_types: {}, projects: {} },
      findings: [],
      checkpoint: null,
      downtime_started_at: null,
      downtime_ended_at: null,
      actual_downtime_s: null,
      approvals: [],
      cutover_requested: false,
      cutover_requested_by: null,
      cutover_requested_at: null,
      force_window: false,
      advisor_notes: [],
      advisor_hold: null,
      last_recommendation: null,
      converged_by: null,
      force_cutover: null,
      data_issues: [],
      cutover_refusals: [],
      downtime_model_s: null,
      verification: null,
      suggested_probes: [],
      source_running_at: null,
      review_required: false,
      review_reason: null,
      destination_server_id: null,
      error: null,
      attempts: 0,
      attempt_strategies: [],
      created_at: created,
      updated_at: created,
    };
    this.migrations.push(m);
    this.emit({ kind: 'migration.created', plan_id: plan.id, migration_id: m.id, actor, message: `${vm.name}: migration created`, data: { strategy: m.strategy } });
    return m;
  }

  private validatePlan(me: Me, plan: Plan): MockResponse {
    // like orchestrator.validate_plan (SDD §8.1, §16): a completed plan is validated again (audit DB-01)
    if (plan.status === 'running') throw new HttpError(409, 'conflict', 'pause the plan before validating it again');
    if (plan.status === 'cancelled') throw new HttpError(409, 'conflict', 'a cancelled plan cannot be validated');
    const mine = this.migrations.filter((m) => m.plan_id === plan.id);
    const byVm = new Map(mine.map((m) => [m.vm.source_id, m]));
    const selected = new Set(plan.vm_ids);
    // the API's order of checks (orchestrator.validate_plan, SDD §5.4): nothing changes before they pass
    // like the API (orchestrator._vms, SDD §5.4, §12, review PL-09): only the VMs a validation re-assesses are read from
    // the source - a migration in any other phase counts as its recorded vm -, and one the source no longer reports is a
    // provider error, before anything changes
    const known = new Set(((this.inventories[plan.source_provider_id] ?? []) as VMRef[]).map((v) => v.source_id));
    const readFromSource = (id: string) => {
      const m = byVm.get(id);
      return !m || reassessed(m);
    };
    const gone = plan.vm_ids.find((id) => readFromSource(id) && !known.has(id));
    if (gone !== undefined) throw new HttpError(502, 'provider_error', `VM '${gone}': VM '${gone}' not found`);
    const cancelled = plan.vm_ids.flatMap((id) => (byVm.get(id)?.phase === 'cancelled' ? [byVm.get(id)!.vm.name] : [])).sort();
    if (cancelled.length) {
      throw new HttpError(400, 'bad_request', `${cancelled.length} VM(s) have a cancelled migration: ${cancelled.slice(0, 10).join(', ')}; remove them from vm_ids or create a new plan for them`);
    }
    // a removed VM's failed migration is cancelled too, unless its cutover stopped the source
    const removed = mine.filter((m) => !selected.has(m.vm.source_id) && REMOVABLE.has(m.phase));
    const stopped = removed.filter((m) => Boolean(m.downtime_started_at) && !m.downtime_ended_at).map((m) => m.vm.name).sort();
    if (stopped.length) {
      throw new HttpError(409, 'conflict', `${stopped.slice(0, 10).join(', ')}: the source VM is stopped after a failed cutover; keep the VM in the plan until it is cut over or rolled back`);
    }
    const restarted = removed.filter((m) => m.phase === 'failed' && Boolean(m.downtime_started_at)).map((m) => m.vm.name).sort();
    if (restarted.length) {
      throw new HttpError(409, 'conflict', `${restarted.slice(0, 10).join(', ')}: the source VM was stopped by a failed cutover; keep the VM in the plan until it is rolled back`);
    }
    const held = this.heldElsewhere(plan, plan.vm_ids);
    if (held.size) {
      throw new HttpError(
        409,
        'conflict',
        `${held.size} VM(s) already have a migration in another plan: ${holdersText(held)}; finish, roll back or cancel it there, or remove the VM from vm_ids`,
      );
    }
    const inventory = (this.inventories[plan.source_provider_id] ?? []) as VMRef[];
    const validated: Migration[] = [];
    for (const vmId of plan.vm_ids) {
      const vm = inventory.find((v) => v.source_id === vmId);
      const m = byVm.get(vmId) ?? (vm ? this.createMigration(plan, vm, me.name) : undefined);
      if (!m) continue;
      // like the API (SDD §5.4): a leftover of an interrupted validation is taken over; a ready one whose source is
      // stopped is left as it is, like one in flight - its VM not read again, the advisor not asked (audit OC-02)
      if (reassessed(m)) {
        if (m.phase !== 'validating') this.transition(m, 'validating', 'plan validation started', me.name);
        this.finishValidation(m, plan, me.name);
      } else {
        // like the API (SDD §9.4, review PL-11): left alone, but in its VM's wave in the plan, so waves planned anew
        // reach it before the plan starts - saved, its updated_at stamped, only when that changes it (review RV39-08)
        const wave = plan.waves.find((w) => w.vm_ids.includes(m.vm.source_id))?.id ?? null;
        if (m.wave_id !== wave) {
          m.wave_id = wave;
          m.updated_at = new Date(this.now()).toISOString();
        }
      }
      validated.push(m);
    }
    for (const m of removed) this.transition(m, 'cancelled', 'removed from the plan', me.name);
    plan.status = 'validated';
    plan.updated_at = new Date(this.now()).toISOString();
    const report: ValidationReport = {
      plan_id: plan.id,
      ok: !validated.some((m) => m.phase === 'blocked'),
      migrations: validated.map((m) => ({ migration_id: m.id, vm_name: m.vm.name, strategy: m.strategy, phase: m.phase, findings: m.findings, estimates: m.estimates })),
    };
    this.emit({ kind: 'plan.validated', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" validated (${report.ok ? 'no blockers' : 'blockers found'})`, data: { ok: report.ok } });
    return ok(report);
  }

  private startPlan(me: Me, plan: Plan): MockResponse {
    // a failed plan (pre-staging failed) starts again and pre-stages again, as in the API (SDD §8)
    if (!['validated', 'paused', 'failed'].includes(plan.status)) throw new HttpError(409, 'conflict', `Only validated, paused or failed plans can start (this plan is ${plan.status}).`);
    const blocked = this.migrations.filter((m) => m.plan_id === plan.id && m.phase === 'blocked');
    if (blocked.length) throw new HttpError(409, 'conflict', `${blocked.length} migration(s) are blocked: ${blocked.map((m) => m.vm.name).join(', ')}.`);
    if (plan.waves.length) {
      // like the API (SDD §9.4): a VM added after the waves were planned would start outside every wave's order
      const waveIds = new Set(plan.waves.map((w) => w.id));
      const stray = this.migrations.filter((m) => m.plan_id === plan.id && !WAVE_COMPLETE_PHASES.has(m.phase) && !(m.wave_id && waveIds.has(m.wave_id)));
      if (stray.length) {
        throw new HttpError(409, 'conflict', `${stray.length} migration(s) belong to no wave: ${stray.slice(0, 10).map((m) => m.vm.name).join(', ')}; re-run the automatic wave planning or add them to a wave`);
      }
    }
    plan.status = 'running';
    plan.updated_at = new Date(this.now()).toISOString();
    for (const m of this.migrations.filter((x) => x.plan_id === plan.id && x.phase === 'ready' && !x.advisor_hold && (x.strategy === 'warm' || x.strategy === 'vmware_warm'))) {
      this.transition(m, 'precopy', 'wave started', 'orchestrator');
    }
    this.emit({ kind: 'plan.started', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" started`, data: {} });
    return ok(plan);
  }

  private pausePlan(me: Me, plan: Plan): MockResponse {
    if (plan.status !== 'running') throw new HttpError(409, 'conflict', `Only running plans can be paused (this plan is ${plan.status}).`);
    plan.status = 'paused';
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.paused', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" paused`, data: {} });
    return ok(plan);
  }

  /** POST /plans/{id}/cancel, like the API (SDD §12): retires a plan none of whose migrations started; nothing is deleted. */
  private cancelPlan(me: Me, plan: Plan, input: Record<string, unknown>): MockResponse {
    refuseCancelBody(input); // the API refuses the body before its route runs
    if (plan.status === 'cancelled') return ok(plan);
    if (plan.status === 'running') throw new HttpError(409, 'conflict', 'pause the plan before cancelling it');
    const mine = this.migrations.filter((m) => m.plan_id === plan.id);
    const started = mine.filter((m) => !['pending', 'blocked', 'ready', 'cancelled'].includes(m.phase));
    if (started.length) {
      const named = started.map((m) => `${m.vm.name} (${m.phase})`).sort();
      const rest = named.length - 10;
      throw new HttpError(
        409,
        'conflict',
        `only a plan whose migrations have not started can be cancelled: ${named.slice(0, 10).join(', ')}${rest > 0 ? ` and ${rest} more` : ''}; ` +
          "cancel a failed or rolled-back migration on its own first, which cleans up what it copied - on VMware it names the kit's copies it keeps",
      );
    }
    // as Python's strip leaves it, like the API (SDD §12, review RV52-S3)
    const reason = typeof input.reason === 'string' && pyStrip(input.reason) ? pyStrip(input.reason) : null;
    const todo = mine.filter((m) => m.phase !== 'cancelled').sort((a, b) => (a.id < b.id ? -1 : 1));
    // like the API (SDD §12): refused before any change while a source is stopped - a cancel cannot restart it
    const stopped = todo.filter((m) => m.downtime_started_at && !m.downtime_ended_at).map((m) => m.vm.name).sort();
    if (stopped.length) {
      const rest = stopped.length - 10;
      throw new HttpError(
        409,
        'conflict',
        `the source VM is stopped after a failed cutover: ${stopped.slice(0, 10).join(', ')}${rest > 0 ? ` and ${rest} more` : ''}; ` +
          'cut it over before cancelling the plan - a cancel cannot restart it',
      );
    }
    // each one as POST /migrations/{id}/cancel does, with its own event
    // the reason sent is bounded; the one each migration records, prefixed, is not (the API's cancel_plan)
    for (const m of todo) this.migrationAction(me, m, 'cancel', { reason: reason ? `plan cancelled: ${reason}` : 'plan cancelled' }, false);
    plan.status = 'cancelled';
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.cancelled', plan_id: plan.id, migration_id: null, actor: me.name, message: `${plan.name} cancelled`, data: { reason, migrations: todo.length } });
    return ok(plan);
  }

  private setStrategy(me: Me, m: Migration, input: Record<string, unknown>): MockResponse {
    const strategy = input.strategy as Strategy;
    if (!STRATEGIES.includes(strategy)) throw new HttpError(422, 'validation_error', 'Unknown strategy.');
    if (!['pending', 'ready', 'blocked'].includes(m.phase)) throw new HttpError(409, 'conflict', `Strategy can only change in pending, ready or blocked (now ${m.phase}).`);
    if (m.advisor_hold) {
      throw new HttpError(409, 'conflict', `the advisor's decision (${m.advisor_hold}) waits for approval: approve or override it (advisor.approve)`);
    }
    const est: Estimate | undefined = m.estimates.find((e) => e.strategy === strategy);
    if (!est || !est.eligible) throw new HttpError(400, 'bad_request', `${strategy} is not eligible for ${m.vm.name}${est?.reasons.length ? `: ${est.reasons.join('; ')}` : ''}.`);
    // like the API (SDD §12, §9.2, review RV40-01): judged now - a Retry re-assesses no stored estimate, and a handover
    // would leave an earlier attempt's volumes behind
    if (strategy === 'storage_handover' && keptAfterRollback(m)) {
      throw new HttpError(400, 'bad_request', `${strategy} is not eligible for ${m.vm.name}: ${HELD_VOLUMES_REASON}.`);
    }
    m.strategy = strategy;
    m.estimate = est;
    this.clearApprovals(m);
    m.updated_at = new Date(this.now()).toISOString();
    // like the API (SDD §5.4): the choice is the plan's override for the VM, so a re-validation keeps it
    const plan = this.plan(m.plan_id);
    plan.strategy_overrides = { ...plan.strategy_overrides, [m.vm.source_id]: strategy };
    plan.updated_at = m.updated_at;
    this.emit({ kind: 'migration.action', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: strategy set to ${strategy}`, data: { action: 'strategy', strategy } });
    this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `${plan.name}: strategy override for ${m.vm.name}`, data: { strategy_overrides: { [m.vm.source_id]: strategy } } });
    return ok(m);
  }

  private migrationAction(me: Me, m: Migration, action: string, input: Record<string, unknown>, sent = true): MockResponse {
    if (action === 'sync' || action === 'retry') refuseEmptyBody(input);
    for (const field of ['comment', 'reason', 'confirm']) {
      const text = input[field];
      // the API bounds the free text of every action a client sends (SDD §12)
      if (sent && typeof text === 'string' && pyLen(text) > ACTION_TEXT_MAX) {
        throw new HttpError(422, 'validation_error', `${field}: at most ${ACTION_TEXT_MAX} characters.`);
      }
    }
    const plan = this.plan(m.plan_id);
    // stripped as the API's Python strips (str.strip), what the dashboard sends kept as sent (review RV50-06)
    const comment = typeof input.comment === 'string' && pyStrip(input.comment) ? pyStrip(input.comment) : null;
    const record = (message: string, data: Record<string, unknown> = {}) =>
      this.emit({ kind: 'migration.action', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: ${message}`, data: { action, ...data } });
    const approve = () => {
      m.approvals.push({ actor: me.name, at: new Date(this.now()).toISOString(), comment });
      this.emit({ kind: 'migration.approved', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: approved by ${me.name}`, data: { comment } });
    };

    switch (action) {
      case 'approve':
        // the API's APPROVABLE and message (SDD §5.4)
        if (!APPROVABLE.has(m.phase)) throw new HttpError(409, 'conflict', `a migration in ${m.phase} cannot be approved`);
        // maker-checker (SDD §5.4, ADV-17): the requester never approves their own cutover
        if (plan.require_distinct_approver && m.cutover_requested_by === me.name) throw new HttpError(409, 'conflict', OWN_CUTOVER);
        // and the approver who forced it (SDD §5.4)
        if (plan.require_distinct_approver && m.force_cutover?.actor === me.name) throw new HttpError(409, 'conflict', OWN_FORCE);
        approve();
        break;
      case 'cutover': {
        // the API's CUTOVER_REQUESTABLE and message: a warm migration requested early cuts over once it converges
        if (!CUTOVER_REQUESTABLE.has(m.phase)) throw new HttpError(409, 'conflict', `cutover cannot be requested in ${m.phase}`);
        // the request is an approval only without a distinct approver (SDD §5.4)
        if (!plan.require_distinct_approver) approve();
        if (!m.cutover_requested_by) {
          m.cutover_requested_by = me.name;
          m.cutover_requested_at = new Date(this.now()).toISOString();
        }
        m.cutover_requested = true;
        m.force_window = m.force_window || Boolean(input.force_window);
        record(input.force_window ? 'cutover requested (window bypassed)' : 'cutover requested', { force_window: Boolean(input.force_window), comment });
        // only a converged warm migration or a ready single-shot one enters the cutover (SDD §5.4); an
        // early request waits for convergence
        const waiting = m.phase === 'awaiting_cutover' || (m.phase === 'ready' && SINGLE_SHOT.has(m.strategy));
        if (plan.status === 'running' && waiting && this.mayBegin(m, plan) && this.gateOpen(m, plan)) this.beginCutover(m, plan);
        break;
      }
      case 'force-cutover': {
        // the API's ForceCutoverRequest and force_cutover (SDD §5.4, §12, ADV-16)
        // stripped and counted as the API's Python does: code points (audit AD-10)
        const reason = typeof input.reason === 'string' ? pyStrip(input.reason) : '';
        if (pyLen(reason) < FORCE_REASON_MIN) throw new HttpError(422, 'validation_error', `reason: at least ${FORCE_REASON_MIN} characters.`);
        if (!FORCEABLE.has(m.phase) || SINGLE_SHOT.has(m.strategy)) {
          throw new HttpError(409, 'conflict', `a Force cutover applies to a warm migration in pre-copy or syncing, not to ${m.strategy} in ${m.phase}`);
        }
        // forced once, like the API (SDD §5.4): a second forcer would take the first one's place
        if (m.force_cutover) throw new HttpError(409, 'conflict', `Already forced by ${m.force_cutover.actor}: the cutover follows the running pass.`);
        const last = m.sync_passes.filter((p) => p.ended_at !== null).at(-1);
        const at = new Date(this.now()).toISOString();
        // the API's figure (SDD §5.4): the downtime after the running pass, as the Force cutover dialog shows it
        const force = { actor: me.name, at, reason, delta_bytes: last?.bytes_changed ?? 0, estimated_downtime_s: forcedDowntime(m, plan) };
        m.force_cutover = force;
        // a request too, under maker-checker like POST …/cutover (SDD §5.4, ADV-17)
        if (!plan.require_distinct_approver) m.approvals.push({ actor: me.name, at, comment: reason });
        if (!m.cutover_requested_by) {
          m.cutover_requested_by = me.name;
          m.cutover_requested_at = at;
        }
        m.cutover_requested = true;
        this.emit({
          kind: 'migration.force_cutover',
          plan_id: m.plan_id,
          migration_id: m.id,
          actor: me.name,
          message: `cutover of ${m.vm.name} forced by ${me.name}: ${reason}`,
          // in the audit trail like the API (SDD §14.5): its target is the migration
          data: { target_id: m.id, vm: m.vm.name, reason, delta_bytes: force.delta_bytes, estimated_downtime_s: force.estimated_downtime_s },
        });
        break;
      }
      case 'sync':
        if (m.phase !== 'awaiting_cutover') throw new HttpError(409, 'conflict', 'A delta sync can only be requested while awaiting cutover.');
        this.transition(m, 'syncing', 'keep-warm sync requested', me.name);
        this.startPass(m, 'delta');
        record('delta sync requested');
        break;
      case 'rollback': {
        const reason = typeof input.reason === 'string' ? pyStrip(input.reason) : '';
        if (!reason) throw new HttpError(422, 'validation_error', 'reason is required.');
        if (!canTransition(m.phase, 'rolling_back')) throw new HttpError(409, 'conflict', `Cannot move ${m.vm.name} from ${m.phase} to rolling_back.`);
        // like the API (SDD §8.1): a finalize that began deleting the source is finalized again, never rolled back
        if (m.phase === 'completed' && m.checkpoint === 'finalizing') {
          throw new HttpError(409, 'conflict', 'the finalize began deleting the source VM: finalize it again');
        }
        // like the API (SDD §5.1): without its providers a rollback fails, and a migration whose cutover
        // stopped its source could then be neither rolled back, retried nor cancelled
        const plan = this.plan(m.plan_id);
        for (const [role, id] of [['source', plan.source_provider_id], ['destination', plan.destination_provider_id]] as const) {
          if (!this.providers.some((p) => p.id === id)) {
            throw new HttpError(409, 'conflict', `${m.vm.name} cannot be rolled back: the plan's ${role} provider '${id}' no longer exists`);
          }
        }
        // like the API (SDD §8.1): the rollback starts the source on purpose - a migration that completes again is
        // watched afresh, and the power the demo set goes with it
        m.source_running_at = null;
        this.power.delete(m.vm.source_id);
        this.transition(m, 'rolling_back', reason, me.name);
        m.progress_pct = 0;
        record('rollback requested', { reason });
        break;
      }
      case 'retry': {
        if (m.phase !== 'failed' && m.phase !== 'rolled_back') throw new HttpError(409, 'conflict', `Cannot retry ${m.vm.name} while ${m.phase}.`);
        const plan = this.plan(m.plan_id);
        const held = this.heldElsewhere(plan, [m.vm.source_id]);
        if (held.size) throw new HttpError(409, 'conflict', `${m.vm.name} cannot be retried: a migration in another plan holds the VM: ${holdersText(held)}; finish, roll back or cancel it there first`);
        // only a completed plan's providers can be deleted, and the retry reopens such a plan: it needs them (SDD §5.1, §12)
        for (const [role, id] of [['source', plan.source_provider_id], ['destination', plan.destination_provider_id]] as const) {
          if (!this.providers.some((p) => p.id === id)) {
            throw new HttpError(409, 'conflict', `${m.vm.name} cannot be retried: the plan's ${role} provider '${id}' no longer exists; create a new plan for the VM`);
          }
        }
        // like the API (SDD §9.4, §12, review PL-11): the attempt runs in its VM's wave in the plan, and a VM in no wave of
        // a plan with waves would run at once, outside every wave's order: refused before any change
        const wave = plan.waves.find((w) => w.vm_ids.includes(m.vm.source_id));
        if (plan.waves.length && !wave) {
          throw new HttpError(
            409,
            'conflict',
            `${m.vm.name} cannot be retried: it belongs to no wave of the plan; re-run the automatic wave planning or add it to a wave`,
          );
        }
        m.wave_id = wave?.id ?? null;
        // like the API (SDD §12, §9.2): nothing is assessed anew, but a handover is ineligible while the attempt's
        // destination volumes are held, which the strategy choice and the advisor's options then show
        if (keptAfterRollback(m)) {
          m.estimates = m.estimates.map(heldOut);
          if (m.estimate) m.estimate = heldOut(m.estimate);
        } else {
          // an earlier Retry's mark comes off once nothing is held (review RV43B-02)
          m.estimates = m.estimates.map(heldIn);
          if (m.estimate) m.estimate = heldIn(m.estimate);
        }
        const reopen = plan.status === 'completed';
        this.transition(m, 'ready', 'retry requested', me.name);
        m.attempts += 1;
        // the strategy of the attempt being retried: the evidence names it, even after a later validation chose another (SDD §14.4)
        m.attempt_strategies = [...m.attempt_strategies, m.strategy];
        if (!m.downtime_started_at || m.downtime_ended_at) {
          // a closed clock starts afresh, with the model seconds the next cutover reports; an open one means the
          // source is still stopped, and keeps its model seconds (SDD §5.2)
          m.downtime_started_at = null;
          m.downtime_ended_at = null;
          m.actual_downtime_s = null;
          m.downtime_model_s = null;
        }
        m.error = null;
        clearRequest(m);
        // like the API's retry (SDD §5.1): what the earlier attempt found about itself goes with it
        m.data_issues = [];
        m.converged_by = null;
        m.review_required = false;
        m.review_reason = null;
        m.checkpoint = null;
        m.progress_pct = 0;
        if (reopen) {
          plan.status = 'running';
          plan.updated_at = new Date(this.now()).toISOString();
          this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `${plan.name} reopened: retry of ${m.vm.name}`, data: { status: 'running' } });
        }
        // the attempts the retry wrote, like the API's migration.action (SDD §5.1, review RV52-O6)
        record('retry requested', { attempts: m.attempts });
        break;
      }
      case 'cancel': {
        if (m.phase === 'validating') {
          // like the API (SDD §5.1): a validation may be moving it
          throw new HttpError(409, 'conflict', `${m.vm.name} is being validated and cannot be cancelled; validate the plan again - it finishes the migration, or cancels it once its VM left vm_ids`);
        }
        if (m.downtime_started_at && !m.downtime_ended_at) {
          // SDD §5.1: never leave a stopped source behind
          const advice = m.phase === 'failed' ? 'roll back or retry' : 'cut it over';
          throw new HttpError(409, 'conflict', `${m.vm.name} cannot be cancelled: the source VM is stopped; ${advice} instead.`);
        }
        if (m.phase === 'failed' && m.downtime_started_at) throw new HttpError(409, 'conflict', 'The source VM was stopped; roll back instead of cancelling.');
        const reason = typeof input.reason === 'string' && pyStrip(input.reason) ? pyStrip(input.reason) : 'cancelled by operator';
        // like the API (SDD §5.1): the data copied so far is cleaned up after the cancel - a pass that runs, or the copies
        // the API reads as kept where no record tells (its `copied_data`, §9.3, review RV43B-03), not any recorded pass
        const cleanup = m.phase === 'precopy' || m.phase === 'syncing' || keptAfterRollback(m);
        this.transition(m, 'cancelled', reason, me.name);
        record('cancelled', { reason });
        if (cleanup) record('temporary resources of the cancelled migration removed', { action: 'cleanup' });
        break;
      }
      case 'finalize': {
        // like the API (SDD §12): the phase first, then the typed name
        if (m.phase !== 'completed') throw new HttpError(409, 'conflict', `only completed migrations can be finalized (${m.phase})`);
        // like the API (SDD §8.1, audit OC-09): not while the source runs again, unless a finalize began
        // deleting it - and this one deletes it too
        if (m.source_running_at && !(m.checkpoint === 'finalizing' && input.delete_source)) {
          throw new HttpError(409, 'conflict', SOURCE_RUNS_AGAIN);
        }
        if (input.confirm !== m.vm.name) throw new HttpError(400, 'bad_request', `Type the VM name "${m.vm.name}" exactly to finalize.`);
        this.transition(m, 'finalized', input.delete_source ? 'finalized; source deleted' : 'finalized; source kept (stopped)', me.name);
        m.checkpoint = 'finalized'; // the API's checkpoint values (SDD §8.1)
        record('finalized', { delete_source: Boolean(input.delete_source) });
        break;
      }
      default:
        throw new HttpError(404, 'not_found', `Unknown action ${action}.`);
    }
    m.updated_at = new Date(this.now()).toISOString();
    return ok(m);
  }

  private similarIncidents(queryText: string, limit: number): { hits: Array<{ title: string; content: string; score: number }> } {
    const corpus = [
      { title: 'RHEL 6 kernel panic after os-migrate import', content: 'Rebuild the initramfs with virtio_blk and virtio_pci (dracut --add-drivers) before cutover; retry succeeded.' },
      { title: 'Warm pass never converged on a busy PostgreSQL VM', content: 'Change rate (~60 MiB/s) exceeded the link share. Scheduled cutover off-peak and raised max_sync_passes to 8.' },
      { title: 'sshd bound to the old fixed IP after migration', content: 'Remove ListenAddress from sshd_config or use 0.0.0.0; verification tcp:22 then passed.' },
      { title: 'Cinder manage failed: volume type not mapped to the RBD pool', content: 'Add the type to plan.handover.backend_map ("hostgroup@backend#pool") and re-validate.' },
      { title: 'VMware CBT reset after snapshot consolidation', content: 'Consolidation reset CBT; vmware_warm restarted with a full pass. Consolidate snapshots before pre-copy.' },
      { title: 'MTU 1500 guest on Geneve network (1442)', content: 'Large packets dropped after cutover; set the guest MTU to 1442 via cloud-init or DHCP option 26.' },
    ];
    const terms = queryText.toLowerCase().split(/\W+/).filter((t) => t.length > 2);
    const scored = corpus
      .map((doc) => {
        const text = `${doc.title} ${doc.content}`.toLowerCase();
        const hits = terms.filter((t) => text.includes(t)).length;
        return { ...doc, score: terms.length ? Math.round((0.35 + (0.6 * hits) / terms.length) * 100) / 100 : 0.35 };
      })
      .filter((doc) => !terms.length || doc.score > 0.35)
      .sort((a, b) => b.score - a.score);
    return { hits: scored.slice(0, Math.max(1, Math.min(20, limit))) };
  }

  stats(planId: string | null): Stats {
    const mine = this.migrations.filter((m) => !planId || m.plan_id === planId);
    const byPhase: Partial<Record<Phase, number>> = {};
    for (const m of mine) byPhase[m.phase] = (byPhase[m.phase] ?? 0) + 1;
    // like the API (SDD §5.5): verified downtimes only, and a migration whose numbers do not hold is counted, not measured
    const done = mine.filter((m) => m.phase === 'completed' || m.phase === 'finalized');
    const downtimes = done.filter((m) => m.actual_downtime_s !== null && m.data_issues.length === 0);
    const values = downtimes.map((m) => m.actual_downtime_s ?? 0).sort((a, b) => a - b);
    const sloOf = (m: Migration) => this.plans.find((p) => p.id === m.plan_id)?.downtime_slo_s ?? 600;
    const byStrategy: Partial<Record<Strategy, number>> = {};
    for (const s of STRATEGIES) {
      const list = downtimes.filter((m) => m.strategy === s).map((m) => m.actual_downtime_s ?? 0);
      if (list.length) byStrategy[s] = Math.round((list.reduce((a, b) => a + b, 0) / list.length) * 10) / 10;
    }
    const active = mine.filter((m) => ['precopy', 'syncing', 'cutover'].includes(m.phase)).length;
    const now = this.now();
    const minute = Math.floor(now / 60_000);
    const series = Array.from({ length: 60 }, (_, i) => {
      const bucket = minute - 59 + i;
      const wave = 0.55 + 0.25 * Math.sin(bucket / 7) + 0.15 * Math.sin(bucket / 2.3);
      const load = Math.min(1, (active + 1) / 4);
      return { ts: iso(bucket * 60_000), bps: Math.max(0, Math.round(131072000 * wave * load)) };
    });
    return {
      total: mine.length,
      by_phase: byPhase,
      completed: mine.filter((m) => m.phase === 'completed' || m.phase === 'finalized').length,
      failed: mine.filter((m) => m.phase === 'failed').length,
      in_progress: mine.filter((m) => ['validating', 'precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'rolling_back'].includes(m.phase)).length,
      bytes_transferred: mine.reduce((sum, m) => sum + m.bytes_transferred, 0),
      avg_downtime_s: values.length ? Math.round((values.reduce((a, b) => a + b, 0) / values.length) * 10) / 10 : null,
      p95_downtime_s: percentile(values, 95),
      max_downtime_s: values.length ? (values.at(-1) ?? null) : null,
      slo_compliance_pct: downtimes.length ? Math.round((downtimes.filter((m) => (m.actual_downtime_s ?? 0) <= sloOf(m)).length / downtimes.length) * 1000) / 10 : null,
      data_inconsistent: done.filter((m) => m.data_issues.length > 0).length,
      awaiting_finalize: mine.filter((m) => m.phase === 'completed').length,
      finalized: mine.filter((m) => m.phase === 'finalized').length,
      downtime_by_strategy: byStrategy,
      throughput_series: series,
    };
  }

  /** `GET /events/stream` as a real `text/event-stream` body. */
  openStream(query: URLSearchParams, token: string | null, signal: AbortSignal | null | undefined, heartbeatMs: number): Response {
    this.request = { method: 'GET', path: '/events/stream' };
    try {
      this.require(token, 'viewer', '/events/stream');
    } catch (error) {
      if (!(error instanceof HttpError)) throw error;
      return new Response(JSON.stringify({ error: { code: error.code, message: error.message } }), {
        status: error.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    const encoder = new TextEncoder();
    let cleanup: () => void = () => {};
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        let closed = false;
        const send = (text: string) => {
          if (closed) return;
          try {
            controller.enqueue(encoder.encode(text));
          } catch {
            cleanup();
          }
        };
        const sinceParam = query.get('since');
        if (sinceParam !== null) {
          const since = Number(sinceParam) || 0;
          for (const event of this.events) if (event.seq > since) send(sseFrame(event));
        }
        const unsubscribe = this.subscribe((event) => send(sseFrame(event)));
        const heartbeat = setInterval(() => send(': heartbeat\n\n'), heartbeatMs);
        cleanup = () => {
          if (closed) return;
          closed = true;
          unsubscribe();
          clearInterval(heartbeat);
          try {
            controller.close();
          } catch {
            // already closed
          }
        };
        signal?.addEventListener('abort', () => cleanup(), { once: true });
      },
      cancel: () => cleanup(),
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } });
  }
}

export interface MockFetchOptions {
  /** Simulated network latency for JSON routes. */
  latencyMs?: number;
  heartbeatMs?: number;
  basePath?: string;
}

function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      },
      { once: true },
    );
  });
}

/** A `fetch` implementation backed by `server`. */
export function createMockFetch(server: MockServer, options: MockFetchOptions = {}): typeof fetch {
  const base = options.basePath ?? '/api/v1';
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = typeof input === 'object' && 'url' in input && !(input instanceof URL) ? input : null;
    const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(href, 'http://mock.local');
    const method = (init?.method ?? request?.method ?? 'GET').toUpperCase();
    const headers = new Headers(init?.headers ?? request?.headers);
    const auth = headers.get('Authorization');
    const token = auth?.startsWith('Bearer ') ? auth.slice(7) : null;
    if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (!url.pathname.startsWith(base)) return new Response('Not found', { status: 404 });
    const path = url.pathname.slice(base.length) || '/';

    if (method === 'GET' && path === '/events/stream') return server.openStream(url.searchParams, token, init?.signal, options.heartbeatMs ?? 15_000);

    if (options.latencyMs) await delay(options.latencyMs, init?.signal);
    let body: unknown;
    if (typeof init?.body === 'string' && init.body) {
      try {
        body = JSON.parse(init.body);
      } catch {
        return new Response(JSON.stringify({ error: { code: 'validation_error', message: 'Request body is not JSON.' } }), { status: 422 });
      }
    }
    const result = server.handle(method, path, url.searchParams, body, token);
    if (result.status === 204) return new Response(null, { status: 204 });
    if (result.text !== undefined) return new Response(result.text, { status: result.status, headers: { 'Content-Type': 'text/plain' } });
    return new Response(JSON.stringify(result.body), { status: result.status, headers: { 'Content-Type': 'application/json' } });
  };
}
