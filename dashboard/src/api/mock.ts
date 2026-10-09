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
import { canTransition } from '../lib/fsm';
import { hasRole } from '../lib/roles';
import { stable } from '../lib/stable';
import { endedPassBytes } from '../lib/transfer';
import {
  buildFixtures,
  defaultPlanFields,
  eligibility,
  estimate,
  GiB,
  iso,
  MiB,
  preflight,
  prng,
} from './mockData';
import type {
  Health,
  AdvisorStatus,
  DestinationInventory,
  Estimate,
  Event,
  Me,
  Migration,
  Phase,
  Plan,
  PlanCreate,
  Provider,
  Role,
  Stats,
  Strategy,
  SyncPass,
  ValidationReport,
  VMRef,
  Wave,
} from './types';
import { ACTION_TEXT_MAX, PHASES, PLAN_DESCRIPTION_MAX, PLAN_NAME_MAX, STRATEGIES, SYNC_PASSES_LATEST } from './types';

interface MockResponse {
  status: number;
  body?: unknown;
  text?: string;
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
}

/** Phases in which a migration lets its VM go: another plan may migrate it (SDD §5.4), as in the API. */
const RELEASES_VM: ReadonlySet<Phase> = new Set(['cancelled', 'finalized', 'rolled_back']);
/** Phases validation evaluates again; a VM taken out of the plan has its migration cancelled in these. */
const REVALIDATABLE: ReadonlySet<Phase> = new Set(['pending', 'blocked', 'ready']);
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
const POLICY_FIELDS = ['require_approval', 'auto_cutover', 'cutover_window'] as const;
const PROVIDER_EDITABLE = new Set(['name', 'endpoint', 'cloud', 'credentials_secret', 'region', 'verify_tls', 'ca_cert_path', 'conversion_host', 'distribution']);
const DISTRIBUTION_KIND: Record<string, string> = { openstack_community: 'openstack', kolla: 'openstack', rhosp: 'openstack', rhoso: 'rhoso', vmware: 'vmware' };

function checkDistribution(input: Record<string, unknown> | Provider) {
  const distribution = (input as Record<string, unknown>).distribution;
  const kind = (input as Record<string, unknown>).kind;
  if (distribution && DISTRIBUTION_KIND[String(distribution)] !== kind) {
    throw new HttpError(422, 'validation_error', `distribution ${String(distribution)} belongs to kind ${DISTRIBUTION_KIND[String(distribution)] ?? 'unknown'}, not ${String(kind)}`);
  }
}

export class MockServer {
  /** What GET /health answers; tests flip it to a degraded state. */
  health: Health = {
    status: 'ok',
    version: '0.1.0-mock',
    demo: true,
    db: 'ok',
    orchestrator: { running: true, last_tick_age_s: 0.4, ticks: 1200, healthy: true },
  };
  readonly providers: Provider[];
  readonly inventories: Record<string, VMRef[] | DestinationInventory>;
  readonly plans: Plan[];
  readonly migrations: Migration[];
  readonly events: Event[];
  private seq: number;
  private readonly listeners = new Set<(event: Event) => void>();
  private readonly rand: () => number;
  private readonly now: () => number;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** The bytes each pass moves (`<migration id>#<pass number>`), drawn once per pass. */
  private readonly passSizes = new Map<string, number>();
  /** The request being handled, for the `auth.denied` audit (SDD §13.1). */
  private request = { method: 'GET', path: '/' };

  constructor(options: MockServerOptions = {}) {
    this.now = options.now ?? (() => Date.now());
    this.rand = prng(options.seed ?? 42);
    const fixtures = buildFixtures(this.now());
    this.providers = fixtures.providers;
    this.inventories = fixtures.inventories;
    this.plans = fixtures.plans;
    this.migrations = fixtures.migrations;
    this.events = fixtures.events;
    this.seq = this.events.at(-1)?.seq ?? 0;
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
          if (plan.status === 'running' && SINGLE_SHOT.has(m.strategy) && this.gateOpen(m, plan)) this.beginCutover(m, plan);
          break;
        case 'awaiting_cutover':
          if (plan.status === 'running' && this.gateOpen(m, plan)) this.beginCutover(m, plan);
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
    return m.sync_passes.find((p) => p.ended_at === null);
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
    m.sync_passes.push(pass);
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
    const pass = this.openPass(m) ?? this.startPass(m, m.sync_passes.length === 0 ? 'full' : 'delta');
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
    if (m.phase === 'precopy') {
      this.transition(m, 'syncing', 'pass 1 complete; delta passes until convergence', 'orchestrator');
      this.startPass(m, 'delta');
      return;
    }
    const converged = pass.bytes_changed <= plan.convergence_threshold_bytes || m.sync_passes.length >= plan.max_sync_passes;
    if (converged) {
      m.progress_pct = 100;
      this.transition(m, 'awaiting_cutover', 'converged: last delta below the threshold', 'orchestrator');
    } else {
      this.startPass(m, 'delta');
    }
  }

  private gateOpen(m: Migration, plan: Plan): boolean {
    const approved = !plan.require_approval || m.approvals.length > 0;
    const requested = plan.auto_cutover || m.cutover_requested;
    const window = plan.cutover_window;
    const now = this.now();
    const inWindow = !window || (now >= Date.parse(window.start) && now <= Date.parse(window.end)) || m.cutover_requested;
    const cutovers = this.migrations.filter((x) => x.phase === 'cutover').length;
    return approved && requested && inWindow && cutovers < 3;
  }

  private beginCutover(m: Migration, plan: Plan): void {
    this.transition(m, 'cutover', 'cutover gate satisfied', 'orchestrator');
    if (!m.downtime_started_at || m.downtime_ended_at) {
      // a retried cutover keeps the clock of the first stop (SDD §5.2)
      m.downtime_started_at = new Date(this.now()).toISOString();
      m.downtime_ended_at = null;
      m.actual_downtime_s = null;
    }
    m.progress_pct = 0;
    this.emit({ kind: 'migration.downtime_started', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: source VM stopped — downtime clock started`, data: {} });
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
    const pass = this.openPass(m);
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
      keepSyncHistory(m, plan.max_sync_passes);
      m.bytes_transferred = endedPassBytes(m);
      this.emit({ kind: 'migration.sync_pass', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: ${pass.kind} pass changed ${(pass.bytes_changed / GiB).toFixed(2)} GiB`, data: { ...pass } });
    }
    m.destination_server_id = `${hex(this.rand, 8)}-${hex(this.rand, 4)}-4${hex(this.rand, 3)}-a${hex(this.rand, 3)}-${hex(this.rand, 12)}`;
    m.checkpoint = 'cutover';
    this.transition(m, 'verifying', 'destination server created', 'orchestrator');
  }

  private finishVerification(m: Migration, plan: Plan): void {
    if (this.rand() < 0.1) {
      m.error = 'Verification failed: tcp:22 connection refused after 600 s.';
      this.emit({ kind: 'migration.error', plan_id: plan.id, migration_id: m.id, actor: 'orchestrator', message: `${m.vm.name}: ${m.error}`, data: {} });
      this.transition(m, 'failed', 'verification failed', 'orchestrator');
      m.advisor_notes.push({
        kind: 'similar_incidents',
        source: 'memory',
        summary: '1 similar incident: sshd bound to the old fixed IP after migration.',
        confidence: null,
        data: { hits: [{ title: 'sshd ListenAddress pinned to the source IP', content: 'Remove ListenAddress or use 0.0.0.0; re-run verification.', score: 0.77 }] },
        created_at: new Date(this.now()).toISOString(),
      });
      this.emit({ kind: 'advisor.similar_incidents', plan_id: plan.id, migration_id: m.id, actor: 'agentmemory', message: `${m.vm.name}: 1 similar incident found`, data: {} });
      if (plan.verification.auto_rollback) {
        m.progress_pct = 0;
        this.transition(m, 'rolling_back', 'automatic rollback after failed verification', 'orchestrator');
      }
      return;
    }
    m.checkpoint = 'verify';
    this.transition(m, 'completed', 'verification passed', 'orchestrator');
    this.endDowntime(m);
    this.emit({ kind: 'memory.lesson_saved', plan_id: plan.id, migration_id: m.id, actor: 'agentmemory', message: `Lesson saved: ${m.strategy}, actual downtime ${Math.round(m.actual_downtime_s ?? 0)} s`, data: { type: 'fact' } });
    this.maybeCompletePlan(plan);
  }

  private endDowntime(m: Migration): void {
    if (!m.downtime_started_at) return;
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

  private transition(m: Migration, to: Phase, reason: string, actor: string): void {
    if (!canTransition(m.phase, to)) {
      throw new HttpError(409, 'conflict', `Cannot move ${m.vm.name} from ${m.phase} to ${to}.`);
    }
    const at = new Date(this.now()).toISOString();
    m.phase_history.push({ from_phase: m.phase, to_phase: to, at, reason, actor });
    const from = m.phase;
    m.phase = to;
    m.updated_at = at;
    this.emit({ kind: 'migration.phase', plan_id: m.plan_id, migration_id: m.id, actor, message: `${m.vm.name}: ${from} → ${to}`, data: { from_phase: from, to_phase: to, reason } });
  }

  private sourceFor(plan: Plan): Provider {
    const provider = this.providers.find((p) => p.id === plan.source_provider_id);
    if (!provider) throw new HttpError(404, 'not_found', `Provider ${plan.source_provider_id} not found.`);
    return provider;
  }

  private destinationFor(plan: Plan): Provider {
    const provider = this.providers.find((p) => p.id === plan.destination_provider_id);
    if (!provider) throw new HttpError(404, 'not_found', `Provider ${plan.destination_provider_id} not found.`);
    return provider;
  }

  private evaluate(m: Migration, plan: Plan): void {
    const source = this.sourceFor(plan);
    const destination = this.destinationFor(plan);
    const dst = this.inventories[destination.id];
    const networks = dst && !Array.isArray(dst) ? dst.networks : {};
    const names = this.migrations.filter((x) => x.plan_id === plan.id).map((x) => x.vm.name);
    m.findings = preflight(m.vm, source.kind, plan, networks, names);
    const elig = eligibility(m.vm, source.kind, plan, source, destination, m.findings);
    m.estimates = (Object.keys(elig) as Strategy[]).map((s) => estimate(m.vm, s, plan, elig[s] ?? []));
    m.strategy = this.select(m, plan);
    m.estimate = m.estimates.find((e) => e.strategy === m.strategy) ?? null;
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
    m.cutover_requested = false;
    m.force_window = false;
  }

  private finishValidation(m: Migration, plan: Plan, actor: string): void {
    this.evaluate(m, plan);
    this.clearApprovals(m);
    const blocked = m.findings.some((f) => f.severity === 'blocker');
    this.transition(m, blocked ? 'blocked' : 'ready', blocked ? 'blocker findings present' : 'pre-flight passed', actor);
  }

  // ---------------------------------------------------------------------------------------
  // HTTP
  // ---------------------------------------------------------------------------------------

  principal(token: string | null): Me | null {
    if (!token) return { name: 'anonymous', role: 'admin' };
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
    if (!plan) throw new HttpError(404, 'not_found', `Plan ${id} not found.`);
    return plan;
  }

  private migration(id: string): Migration {
    const migration = this.migrations.find((m) => m.id === id);
    if (!migration) throw new HttpError(404, 'not_found', `Migration ${id} not found.`);
    return migration;
  }

  private route(method: string, path: string, query: URLSearchParams, body: unknown, token: string | null): MockResponse {
    const seg = path.split('/').filter(Boolean);
    const [root, id, sub, subsub] = seg;
    const input = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;

    if (method === 'GET' && root === 'health') return ok(this.health);

    if (root === 'me' && method === 'GET') return ok(this.require(token, 'viewer', path));

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
          this.require(token, 'viewer', path);
          throw new HttpError(404, 'not_found', `Provider ${id} not found.`);
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
          const inUse = this.plans.some(
            (p) => (p.source_provider_id === id || p.destination_provider_id === id) && !['completed', 'failed'].includes(p.status),
          );
          if (inUse) throw new HttpError(409, 'conflict', `${provider.name} is referenced by a plan that is not finished.`);
          this.providers.splice(this.providers.indexOf(provider), 1);
          this.emit({ kind: 'provider.deleted', plan_id: null, migration_id: null, actor: me.name, message: `${provider.name} deleted`, data: {} });
          return { status: 204 };
        }
        if (sub === 'check' && method === 'POST') {
          const me = this.require(token, 'operator', path);
          provider.last_checked_at = new Date(this.now()).toISOString();
          if (provider.status === 'unknown') {
            // a mock convention: an endpoint whose host starts with "unreachable" fails the test
            if (/^https?:\/\/unreachable[.-]/i.test(provider.endpoint)) {
              provider.status = 'error';
              provider.status_message = 'The endpoint did not answer (connection timed out).';
            } else {
              provider.status = 'ok';
              provider.status_message = 'All services reachable.';
              provider.capabilities = { admin: true, compute_microversion: '2.95', ovn: true, volume_backends: ['ceph-ssd'] };
            }
          }
          this.emit({ kind: 'provider.checked', plan_id: null, migration_id: null, actor: me.name, message: `${provider.name}: ${provider.status}`, data: { status: provider.status } });
          return ok(provider);
        }
        if (sub === 'inventory' && method === 'GET') {
          this.require(token, 'viewer', path);
          if (provider.status === 'error') throw new HttpError(502, 'provider_error', `${provider.name}: ${provider.status_message ?? 'unreachable'}`);
          return ok(this.inventories[provider.id] ?? (provider.role === 'source' ? [] : { networks: {}, flavors: [], volume_types: [], quotas: {}, projects: [] }));
        }
      }
    }

    if (root === 'plans') {
      if (!id) {
        if (method === 'GET') {
          this.require(token, 'viewer', path);
          // SDD §12: status filter and limit/offset paging, like the migrations list
          const rows = this.plans.filter((p) => !query.get('status') || p.status === query.get('status'));
          const offset = Number(query.get('offset') ?? 0);
          const limit = query.get('limit') === null ? rows.length : Number(query.get('limit'));
          return ok(rows.slice(offset, offset + limit));
        }
        if (method === 'POST') return this.createPlan(this.require(token, 'operator', path), input);
      } else {
        if (!sub && method === 'GET') {
          this.require(token, 'viewer', path);
          return ok(this.plan(id));
        }
        if (!sub && method === 'PATCH') return this.patchPlan(this.require(token, 'operator', path), this.plan(id), input);
        if (sub === 'waves' && subsub === 'auto' && method === 'POST') return this.autoWaves(this.require(token, 'operator', path), this.plan(id), input);
        if (sub === 'validate' && method === 'POST') return this.validatePlan(this.require(token, 'operator', path), this.plan(id));
        if (sub === 'start' && method === 'POST') return this.startPlan(this.require(token, 'operator', path), this.plan(id));
        if (sub === 'pause' && method === 'POST') return this.pausePlan(this.require(token, 'operator', path), this.plan(id));
      }
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
        const offset = Number(query.get('offset') ?? 0);
        const limit = query.get('limit') === null ? rows.length : Number(query.get('limit'));
        return ok(rows.slice(offset, offset + limit));
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
        const min: Role = sub === 'approve' || sub === 'cutover' || sub === 'finalize' ? 'approver' : 'operator';
        const me = this.require(token, min, path);
        return this.migrationAction(me, this.migration(id), sub, input);
      }
    }

    if (root === 'events' && method === 'GET' && !id) {
      this.require(token, 'viewer', path);
      const since = Number(query.get('since') ?? 0) || 0;
      const limit = Math.min(1000, Math.max(1, Number(query.get('limit') ?? 500) || 500));
      const planId = query.get('plan_id');
      const migrationId = query.get('migration_id');
      const matching = this.events.filter(
        (e) => e.seq > since && (!planId || e.plan_id === planId) && (!migrationId || e.migration_id === migrationId),
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
          jev: { mode: 'http', available: true, last_error: null },
          memory: { enabled: true, available: true, last_error: 'Timed out after 5 s (recovered on retry)' },
        };
        return ok(status);
      }
      if (id === 'similar-incidents' && method === 'POST') {
        this.require(token, 'operator', path);
        return ok(this.similarIncidents(String(input.query ?? ''), Number(input.limit ?? 5) || 5));
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

  private createProvider(me: Me, input: Record<string, unknown>): MockResponse {
    const id = String(input.id ?? '');
    if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(id)) throw new HttpError(422, 'validation_error', 'id: must match ^[a-z0-9][a-z0-9-]{1,62}$.');
    if (this.providers.some((p) => p.id === id)) throw new HttpError(409, 'conflict', `provider '${id}' already exists`);
    checkDistribution(input);
    const defaults: Partial<Provider> = {
      cloud: null,
      credentials_secret: null,
      region: null,
      verify_tls: true,
      ca_cert_path: null,
      conversion_host: null,
      distribution: null,
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
    this.emit({ kind: 'provider.created', plan_id: null, migration_id: null, actor: me.name, message: `${provider.name} registered`, data: {} });
    return ok(provider, 201);
  }

  /** PATCH /providers/{id}: editable fields only, 409 while a running/paused plan uses it. */
  private patchProvider(me: Me, provider: Provider, input: Record<string, unknown>): MockResponse {
    const unknown = Object.keys(input).filter((k) => !PROVIDER_EDITABLE.has(k)).sort();
    if (unknown.length) throw new HttpError(422, 'validation_error', `fields cannot be changed: ${unknown.join(', ')}`);
    const users = this.plans.filter(
      (p) => ['running', 'paused'].includes(p.status) && (p.source_provider_id === provider.id || p.destination_provider_id === provider.id),
    );
    if (users.length) throw new HttpError(409, 'conflict', `provider is used by running or paused plan(s): ${users.map((p) => p.id).join(', ')}`);
    checkDistribution({ ...provider, ...input });
    Object.assign(provider, input, { status: 'unknown', status_message: null, last_checked_at: null, capabilities: {} });
    this.emit({ kind: 'provider.updated', plan_id: null, migration_id: null, actor: me.name, message: `provider ${provider.id} updated`, data: { provider_id: provider.id, fields: Object.keys(input).sort() } });
    return ok(provider);
  }

  /** PUT /providers/{id}/credentials: the mock keeps no values, only the "stored" stamp. */
  private setCredentials(me: Me, provider: Provider, input: Record<string, unknown>): MockResponse {
    const values = Object.fromEntries(Object.entries(input).filter(([, v]) => typeof v === 'string' && v.trim())) as Record<string, string>;
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
    const key = String(input.private_key ?? '').trim();
    if (!key.startsWith('-----BEGIN') || !key.includes('PRIVATE KEY-----')) throw new HttpError(422, 'validation_error', 'private_key must be an OpenSSH or PEM private key');
    const host = provider.conversion_host ?? { manage: false, name: null, flavor: null, external_network: null, image: null, ssh_user: 'cloud-user', address: null, ssh_allowed_cidr: null, ssh_key_secret: null };
    Object.assign(provider, {
      conversion_host: { ...host, ssh_key_secret: `provider-${provider.id}-ssh` },
      conversion_key_updated_at: new Date(this.now()).toISOString(),
      status: 'unknown',
      status_message: null,
      last_checked_at: null,
      capabilities: {},
    });
    this.emit({ kind: 'provider.credentials_updated', plan_id: null, migration_id: null, actor: me.name, message: `conversion-host key of provider ${provider.id} updated`, data: { provider_id: provider.id, keys: ['private_key'] } });
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

  private createPlan(me: Me, input: Record<string, unknown>): MockResponse {
    this.refusePolicyChange(me, input, defaultPlanFields(this.now()));
    this.refuseLongPlanTexts(input);
    const body = input as unknown as PlanCreate;
    if (!body.name || !String(body.name).trim()) throw new HttpError(422, 'validation_error', 'name is required.');
    if (!body.source_provider_id || !body.destination_provider_id) throw new HttpError(422, 'validation_error', 'source_provider_id and destination_provider_id are required.');
    if (!Array.isArray(body.vm_ids) || body.vm_ids.length === 0) throw new HttpError(422, 'validation_error', 'Select at least one VM.');
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

  /** SDD §12: no plan edit or re-wave while a migration of the plan is in flight (409). */
  private refuseInFlight(plan: Plan, what: string): void {
    const inFlight = new Set(['precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying', 'rolling_back', 'completed']);
    const busy = this.migrations
      .filter((m) => m.plan_id === plan.id && (inFlight.has(m.phase) || (Boolean(m.downtime_started_at) && !m.downtime_ended_at)))
      .map((m) => m.vm.name)
      .sort();
    if (busy.length) throw new HttpError(409, 'conflict', `migrations in flight: ${busy.slice(0, 10).join(', ')}; finish, roll back or cancel them before ${what}`);
  }

  private patchPlan(me: Me, plan: Plan, input: Record<string, unknown>): MockResponse {
    this.refusePolicyChange(me, input, plan as unknown as Record<string, unknown>);
    this.refuseLongPlanTexts(input);
    if (!['draft', 'validated'].includes(plan.status)) throw new HttpError(409, 'conflict', `Plans can only be edited in draft or validated (this plan is ${plan.status}).`);
    this.refuseInFlight(plan, 'editing the plan');
    for (const key of Object.keys(input)) {
      if (['id', 'waves', 'status', 'created_at', 'updated_at'].includes(key)) continue;
      (plan as unknown as Record<string, unknown>)[key] = input[key];
    }
    plan.status = 'draft';
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}" updated`, data: { fields: Object.keys(input) } });
    return ok(plan);
  }

  private autoWaves(me: Me, plan: Plan, input: Record<string, unknown>): MockResponse {
    if (!['draft', 'validated'].includes(plan.status)) throw new HttpError(409, 'conflict', `Waves can only be planned in draft or validated (this plan is ${plan.status}).`);
    this.refuseInFlight(plan, 're-planning the waves');
    const size = Math.max(1, Math.min(100, Number(input.max_wave_size ?? 10) || 10));
    const mine = this.migrations.filter((m) => m.plan_id === plan.id && m.phase !== 'cancelled').sort((a, b) => a.vm.disk_bytes - b.vm.disk_bytes);
    const pilot = mine.slice(0, Math.min(3, mine.length));
    const rest = mine.slice(pilot.length);
    const waves: Wave[] = [];
    if (pilot.length) waves.push({ id: 'wave-1', name: 'Pilot', order: 1, vm_ids: pilot.map((m) => m.vm.source_id), depends_on: [], max_parallel: 5 });
    for (let i = 0; i < rest.length; i += size) {
      const n = waves.length + 1;
      waves.push({ id: `wave-${n}`, name: `Wave ${n}`, order: n, vm_ids: rest.slice(i, i + size).map((m) => m.vm.source_id), depends_on: [`wave-${n - 1}`], max_parallel: 5 });
    }
    plan.waves = waves;
    for (const wave of waves) for (const m of mine) if (wave.vm_ids.includes(m.vm.source_id)) m.wave_id = wave.id;
    plan.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'plan.updated', plan_id: plan.id, migration_id: null, actor: me.name, message: `Plan "${plan.name}": ${waves.length} waves planned`, data: { waves: waves.length } });
    return ok(plan);
  }

  /**
   * The VMs of `vmIds` that migrations of other plans with the same source hold (SDD §5.4), by source
   * id: `name (plan "…", phase)` once per holder — plans validated before the rule may hold a VM twice.
   */
  private heldElsewhere(plan: Plan, vmIds: readonly string[]): Map<string, string[]> {
    const others = new Map(this.plans.filter((p) => p.id !== plan.id && p.source_provider_id === plan.source_provider_id).map((p) => [p.id, p]));
    const held = new Map<string, Set<string>>();
    for (const m of this.migrations) {
      if (!others.has(m.plan_id) || !vmIds.includes(m.vm.source_id) || RELEASES_VM.has(m.phase)) continue;
      const holders = held.get(m.vm.source_id) ?? new Set<string>();
      holders.add(`${m.vm.name} (plan "${others.get(m.plan_id)?.name}", ${m.phase})`);
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
      force_window: false,
      advisor_notes: [],
      review_required: false,
      review_reason: null,
      destination_server_id: null,
      error: null,
      attempts: 0,
      created_at: created,
      updated_at: created,
    };
    this.migrations.push(m);
    this.emit({ kind: 'migration.created', plan_id: plan.id, migration_id: m.id, actor, message: `${vm.name}: migration created`, data: { strategy: m.strategy } });
    return m;
  }

  private validatePlan(me: Me, plan: Plan): MockResponse {
    if (['running', 'completed'].includes(plan.status)) throw new HttpError(409, 'conflict', `A ${plan.status} plan cannot be re-validated.`);
    const mine = this.migrations.filter((m) => m.plan_id === plan.id);
    const byVm = new Map(mine.map((m) => [m.vm.source_id, m]));
    const selected = new Set(plan.vm_ids);
    // the API's order of checks (orchestrator.validate_plan, SDD §5.4): nothing changes before they pass
    const cancelled = plan.vm_ids.flatMap((id) => (byVm.get(id)?.phase === 'cancelled' ? [byVm.get(id)!.vm.name] : [])).sort();
    if (cancelled.length) {
      throw new HttpError(400, 'bad_request', `${cancelled.length} VM(s) have a cancelled migration: ${cancelled.slice(0, 10).join(', ')}; remove them from vm_ids or create a new plan for them`);
    }
    const stopped = mine
      .filter((m) => !selected.has(m.vm.source_id) && REVALIDATABLE.has(m.phase) && Boolean(m.downtime_started_at) && !m.downtime_ended_at)
      .map((m) => m.vm.name)
      .sort();
    if (stopped.length) {
      throw new HttpError(409, 'conflict', `${stopped.slice(0, 10).join(', ')}: the source VM is stopped after a failed cutover; keep the VM in the plan until it is cut over or rolled back`);
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
      if (REVALIDATABLE.has(m.phase)) {
        this.transition(m, 'validating', 'plan validation started', me.name);
        this.finishValidation(m, plan, me.name);
      }
      validated.push(m);
    }
    for (const m of mine) {
      if (!selected.has(m.vm.source_id) && REVALIDATABLE.has(m.phase)) this.transition(m, 'cancelled', 'removed from the plan', me.name);
    }
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
    plan.status = 'running';
    plan.updated_at = new Date(this.now()).toISOString();
    for (const m of this.migrations.filter((x) => x.plan_id === plan.id && x.phase === 'ready' && (x.strategy === 'warm' || x.strategy === 'vmware_warm'))) {
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

  private setStrategy(me: Me, m: Migration, input: Record<string, unknown>): MockResponse {
    const strategy = input.strategy as Strategy;
    if (!STRATEGIES.includes(strategy)) throw new HttpError(422, 'validation_error', 'Unknown strategy.');
    if (!['pending', 'ready', 'blocked'].includes(m.phase)) throw new HttpError(409, 'conflict', `Strategy can only change in pending, ready or blocked (now ${m.phase}).`);
    const est: Estimate | undefined = m.estimates.find((e) => e.strategy === strategy);
    if (!est || !est.eligible) throw new HttpError(400, 'bad_request', `${strategy} is not eligible for ${m.vm.name}${est?.reasons.length ? `: ${est.reasons.join('; ')}` : ''}.`);
    m.strategy = strategy;
    m.estimate = est;
    this.clearApprovals(m);
    m.updated_at = new Date(this.now()).toISOString();
    this.emit({ kind: 'migration.action', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: strategy set to ${strategy}`, data: { action: 'strategy', strategy } });
    return ok(m);
  }

  private migrationAction(me: Me, m: Migration, action: string, input: Record<string, unknown>): MockResponse {
    for (const field of ['comment', 'reason', 'confirm']) {
      const text = input[field];
      // the API bounds the free text of every action (SDD §12)
      if (typeof text === 'string' && text.length > ACTION_TEXT_MAX) {
        throw new HttpError(422, 'validation_error', `${field}: at most ${ACTION_TEXT_MAX} characters.`);
      }
    }
    const plan = this.plan(m.plan_id);
    const comment = typeof input.comment === 'string' && input.comment.trim() ? input.comment.trim() : null;
    const record = (message: string, data: Record<string, unknown> = {}) =>
      this.emit({ kind: 'migration.action', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: ${message}`, data: { action, ...data } });
    const approve = () => {
      m.approvals.push({ actor: me.name, at: new Date(this.now()).toISOString(), comment });
      this.emit({ kind: 'migration.approved', plan_id: m.plan_id, migration_id: m.id, actor: me.name, message: `${m.vm.name}: approved by ${me.name}`, data: { comment } });
    };

    switch (action) {
      case 'approve':
        if (!['ready', 'precopy', 'syncing', 'awaiting_cutover'].includes(m.phase)) throw new HttpError(409, 'conflict', `Cannot approve ${m.vm.name} while ${m.phase}.`);
        approve();
        break;
      case 'cutover': {
        const allowed = m.phase === 'awaiting_cutover' || (m.phase === 'ready' && SINGLE_SHOT.has(m.strategy));
        if (!allowed) throw new HttpError(409, 'conflict', `Cannot request cutover for ${m.vm.name} while ${m.phase}.`);
        approve();
        m.cutover_requested = true;
        m.force_window = m.force_window || Boolean(input.force_window);
        record(input.force_window ? 'cutover requested (window bypassed)' : 'cutover requested', { force_window: Boolean(input.force_window) });
        if (plan.status === 'running' && this.gateOpen(m, plan)) this.beginCutover(m, plan);
        break;
      }
      case 'sync':
        if (m.phase !== 'awaiting_cutover') throw new HttpError(409, 'conflict', 'A delta sync can only be requested while awaiting cutover.');
        this.transition(m, 'syncing', 'keep-warm sync requested', me.name);
        this.startPass(m, 'delta');
        record('delta sync requested');
        break;
      case 'rollback': {
        const reason = typeof input.reason === 'string' ? input.reason.trim() : '';
        if (!reason) throw new HttpError(422, 'validation_error', 'reason is required.');
        this.transition(m, 'rolling_back', reason, me.name);
        m.progress_pct = 0;
        record('rollback requested', { reason });
        break;
      }
      case 'retry': {
        if (m.phase !== 'failed' && m.phase !== 'rolled_back') throw new HttpError(409, 'conflict', `Cannot retry ${m.vm.name} while ${m.phase}.`);
        const held = this.heldElsewhere(this.plan(m.plan_id), [m.vm.source_id]);
        if (held.size) throw new HttpError(409, 'conflict', `${m.vm.name} cannot be retried: a migration in another plan holds the VM: ${holdersText(held)}; finish, roll back or cancel it there first`);
        this.transition(m, 'ready', 'retry requested', me.name);
        m.attempts += 1;
        if (!m.downtime_started_at || m.downtime_ended_at) {
          // a closed clock starts afresh; an open one means the source is still stopped (SDD §5.2)
          m.downtime_started_at = null;
          m.downtime_ended_at = null;
          m.actual_downtime_s = null;
        }
        m.error = null;
        m.cutover_requested = false;
        m.force_window = false;
        m.progress_pct = 0;
        record('retry requested');
        break;
      }
      case 'cancel': {
        if (m.downtime_started_at && !m.downtime_ended_at) {
          // SDD §5.1: never leave a stopped source behind
          const advice = m.phase === 'failed' ? 'roll back or retry' : 'cut it over';
          throw new HttpError(409, 'conflict', `${m.vm.name} cannot be cancelled: the source VM is stopped; ${advice} instead.`);
        }
        if (m.phase === 'failed' && m.downtime_started_at) throw new HttpError(409, 'conflict', 'The source VM was stopped; roll back instead of cancelling.');
        const reason = typeof input.reason === 'string' && input.reason.trim() ? input.reason.trim() : 'cancelled by operator';
        this.transition(m, 'cancelled', reason, me.name);
        record('cancelled', { reason });
        break;
      }
      case 'finalize': {
        if (input.confirm !== m.vm.name) throw new HttpError(400, 'bad_request', `Type the VM name "${m.vm.name}" exactly to finalize.`);
        this.transition(m, 'finalized', input.delete_source ? 'finalized; source deleted' : 'finalized; source kept (stopped)', me.name);
        m.checkpoint = 'finalize';
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
    const downtimes = mine.filter((m) => m.actual_downtime_s !== null && m.downtime_ended_at !== null);
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
