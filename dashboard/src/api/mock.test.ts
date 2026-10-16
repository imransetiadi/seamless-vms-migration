import { describe, expect, it, vi } from 'vitest';
// the keys an OpenStack or RHOSO check reports, as the API's (SDD §10; plan T8)
import checkKeysRaw from '../../../seamless/tests/fixtures/check_report_keys.json?raw';
import { ApiClient } from './client';
import { canonicalJson, sha256Hex } from '../lib/canonical';
import { forcedDowntime } from '../lib/forcedEstimate';
import { createMockFetch, MockServer, PLAN_EDITABLE_FIELDS } from './mock';
import { summaryOf } from '../lib/summary';
import { streamEvents } from './stream';
import {
  ADVISOR_NOTE_KINDS,
  AUDIT_EVENT_KINDS,
  ADVISOR_NOTE_SOURCES,
  PHASES,
  PLAN_STATUSES,
  PROVIDER_STATUSES,
  SEVERITIES,
  STRATEGIES,
  type AdvisorNote,
  type AdvisorNoteList,
  type MigrationSummary,
  type AdvisorPolicy,
  type AdvisorRecommendation,
  type AdvisorStatus,
  type Event,
  type Migration,
  type Plan,
  type Provider,
  type PlanFinalizeResult,
  type RecommendationPage,
  type Stats,
  type VMRef,
  type Wave,
} from './types';

const NOW = Date.parse('2026-10-08T12:00:00Z');
const CHECK_KEYS = JSON.parse(checkKeysRaw) as { openstack: string[]; destination: string[] };

function setup(token: string | null = 'admin') {
  const server = new MockServer({ now: () => NOW, seed: 1 });
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  return { server, client };
}

/** Inventory VMs of `providerId` that no migration of the fixtures uses: free for a new plan. */
/** Maker-checker (SDD §5.4, ADV-17): an approval by someone other than the approver persona who requested the cutover. */
async function secondApprover(server: MockServer, migrationId: string): Promise<void> {
  const admin = new ApiClient({ getToken: () => 'admin', fetchImpl: createMockFetch(server) });
  await admin.post(`/migrations/${migrationId}/approve`, { comment: 'Second approver: findings and window checked' });
}

function freeVms(server: MockServer, providerId: string, count: number): VMRef[] {
  const inventory = (server as unknown as { inventories: Record<string, VMRef[]> }).inventories[providerId] ?? [];
  const taken = new Set(server.migrations.map((m) => m.vm.source_id));
  const free = inventory.filter((v) => !taken.has(v.source_id)).slice(0, count);
  if (free.length < count) throw new Error(`fixtures have fewer than ${count} free VMs on ${providerId}`);
  return free;
}

function byPhase(server: MockServer, phase: Migration['phase']): Migration {
  const migration = server.migrations.find((m) => m.phase === phase);
  if (!migration) throw new Error(`no fixture in ${phase}`);
  return migration;
}

describe('mock fixtures', () => {
  const server = new MockServer({ now: () => NOW });

  it('exercise every phase, strategy and plan status', () => {
    const phases = new Set(server.migrations.map((m) => m.phase));
    for (const phase of PHASES) expect(phases, phase).toContain(phase);
    const strategies = new Set(server.migrations.map((m) => m.strategy));
    for (const strategy of STRATEGIES) expect(strategies, strategy).toContain(strategy);
    const statuses = new Set(server.plans.map((p) => p.status));
    for (const status of PLAN_STATUSES) expect(statuses, status).toContain(status);
    const providerStatuses = new Set(server.providers.map((p) => p.status));
    for (const status of PROVIDER_STATUSES) expect(providerStatuses, status).toContain(status);
  });

  it('exercise every finding severity and advisor note kind/source', () => {
    const severities = new Set(server.migrations.flatMap((m) => m.findings.map((f) => f.severity)));
    for (const severity of SEVERITIES) expect(severities, severity).toContain(severity);
    const notes = server.migrations.flatMap((m) => m.advisor_notes);
    for (const kind of ADVISOR_NOTE_KINDS) expect(notes.map((n) => n.kind), kind).toContain(kind);
    for (const source of ADVISOR_NOTE_SOURCES) expect(notes.map((n) => n.source), source).toContain(source);
  });

  it('keeps derived VM sizes consistent with SDD §4.2', () => {
    for (const { vm } of server.migrations) {
      const disk = vm.disks.reduce((sum, d) => sum + d.size_gb * 2 ** 30, 0);
      expect(vm.disk_bytes).toBe(disk);
      if (vm.disks.some((d) => d.used_gb === null)) expect(vm.used_bytes).toBe(Math.floor(disk * 0.6));
    }
  });

  it('seed auth.denied events shaped like the API audit (SDD §13.1)', () => {
    const denied = new MockServer({ now: () => NOW, seed: 1 }).events.filter((e) => e.kind === 'auth.denied');
    expect(denied.length).toBeGreaterThan(0);
    for (const e of denied) {
      expect(Object.keys(e.data).sort()).toEqual(['client', 'method', 'path', 'reason', 'required_role']);
      expect(e.message).toBe(`${e.data.method} ${e.data.path}: ${e.data.reason}`);
      expect(e.actor).not.toBe('anonymous');
    }
  });

  it('list only passes that ended and count bytes like the API: the disk, and the passes plus the running step (SDD §4.2)', () => {
    const steps = new Set<Migration['phase']>(['precopy', 'syncing', 'cutover']);
    for (const m of new MockServer({ now: () => NOW, seed: 1 }).migrations) {
      expect(m.bytes_total, m.id).toBe(m.vm.used_bytes);
      expect(m.sync_passes.filter((p) => p.ended_at === null), m.id).toEqual([]);
      if (!m.sync_passes.length) continue;
      const ended = m.sync_bytes_dropped + m.sync_passes.reduce((sum, p) => sum + p.bytes_transferred, 0);
      // a migration mid-step has the running step's bytes on top of its passes
      if (steps.has(m.phase)) expect(m.bytes_transferred, m.id).toBeGreaterThan(ended);
      else expect(m.bytes_transferred, m.id).toBe(ended);
    }
  });

  it('numbers persisted events in increasing order', () => {
    const seqs = server.events.map((e) => e.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(new Set(seqs).size).toBe(seqs.length);
  });
});

describe('mock API', () => {
  it('refuses a plan name over 200 characters and a description over 2000 with 422, like the API (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const base = { source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: [freeVms(server, 'rhosp17-dc1', 1)[0]!.source_id] };
    await expect(client.post('/plans', { ...base, name: 'x'.repeat(201) })).rejects.toMatchObject({ status: 422, message: expect.stringContaining('name') });
    await expect(client.post('/plans', { ...base, name: 'Fine', description: 'd'.repeat(2001) })).rejects.toMatchObject({ status: 422, message: expect.stringContaining('description') });
    const plan = await client.post<Plan>('/plans', { ...base, name: 'x'.repeat(200), description: 'd'.repeat(2000) });
    await expect(client.patch(`/plans/${plan.id}`, { name: 'x'.repeat(201) })).rejects.toMatchObject({ status: 422 });
  });

  it('creates a plan with an empty vm_ids and refuses one without vm_ids, like the API (SDD §12)', async () => {
    const { client } = setup('operator');
    const base = { name: 'Empty for now', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod' };
    // vm_ids is required and must not repeat a VM; an empty list is a plan to fill later
    await expect(client.post<Plan>('/plans', { ...base, vm_ids: [] })).resolves.toMatchObject({ status: 'draft', vm_ids: [] });
    await expect(client.post('/plans', base)).rejects.toMatchObject({ status: 422, code: 'validation_error', message: 'vm_ids: Field required' });
  });

  it('refuses action texts over 2000 characters with 422, like the API (SDD §12)', async () => {
    const { server, client } = setup('approver');
    const m = byPhase(server, 'awaiting_cutover');
    const long = 'x'.repeat(2001);
    for (const [action, body] of [
      ['approve', { comment: long }],
      ['cutover', { comment: long }],
      ['rollback', { reason: long }],
      ['cancel', { reason: long }],
      ['finalize', { confirm: long }],
    ] as const) {
      await expect(client.post(`/migrations/${m.id}/${action}`, body)).rejects.toMatchObject({ status: 422, code: 'validation_error' });
    }
    await expect(client.post<Migration>(`/migrations/${m.id}/approve`, { comment: 'x'.repeat(2000) })).resolves.toMatchObject({ id: m.id });
  });

  it('audits every request refused for its role as auth.denied, like the API (SDD §13.1)', async () => {
    const { server } = setup();
    const as = (token: string) => new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
    const since = server.events.at(-1)?.seq ?? 0;
    const plan = { name: 'Audit', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: ['os-0a11'] };
    await expect(as('nope').get('/plans')).rejects.toMatchObject({ status: 401, message: 'missing or invalid bearer token' });
    await expect(as('locked').get('/plans')).rejects.toMatchObject({ status: 429, code: 'too_many_requests' });
    await expect(as('viewer').post('/plans', plan)).rejects.toMatchObject({ status: 403, message: 'this action requires the operator role' });
    await expect(as('operator').post('/plans', { ...plan, auto_cutover: true })).rejects.toMatchObject({ status: 403 });
    const stream = await createMockFetch(server)('/api/v1/events/stream', { headers: { Authorization: 'Bearer nope' } });
    expect(stream.status).toBe(401);
    // routes without a role check refuse nothing: the health route stays public, even when locked
    await expect(as('locked').get('/health')).resolves.toMatchObject({ status: expect.any(String) });

    const shape = (method: string, path: string, reason: string, required_role: string) => ({
      message: `${method} /api/v1${path}: ${reason}`,
      data: { path: `/api/v1${path}`, method, reason, required_role, client: 'browser' },
    });
    const denied = server.events.filter((e) => e.seq > since && e.kind === 'auth.denied');
    expect(denied.map((e) => ({ actor: e.actor, message: e.message, data: e.data }))).toEqual([
      { actor: 'unauthenticated', ...shape('GET', '/plans', 'missing or invalid bearer token', 'viewer') },
      { actor: 'unauthenticated', ...shape('GET', '/plans', 'too many failed authentication attempts', 'viewer') },
      { actor: 'dimas', ...shape('POST', '/plans', 'role viewer is below operator', 'operator') },
      { actor: 'bayu', ...shape('POST', '/plans', 'setting auto_cutover requires the approver role', 'approver') },
      { actor: 'unauthenticated', ...shape('GET', '/events/stream', 'missing or invalid bearer token', 'viewer') },
    ]);
  });

  it('maps tokens to roles and rejects unknown tokens', async () => {
    await expect(setup('viewer').client.get('/me')).resolves.toEqual({ name: 'dimas', role: 'viewer' });
    await expect(setup(null).client.get('/me')).resolves.toEqual({ name: 'anonymous', role: 'admin' });
    await expect(setup('nonsense').client.get('/me')).rejects.toMatchObject({ status: 401 });
  });

  it('simulates the auth lockout for API routes but keeps the health routes public', async () => {
    const { client } = setup('locked');
    await expect(client.get('/me')).rejects.toMatchObject({ status: 429, code: 'too_many_requests' });
    await expect(client.get('/health')).resolves.toMatchObject({ status: expect.any(String) });
  });

  it('answers GET /events?tail=true with the newest matching events, ascending (SDD §12)', async () => {
    const { server, client } = setup('viewer');
    const target = server.migrations[0] as Migration;
    for (let i = 0; i < 1200; i += 1) {
      server.emit({ kind: 'plan.updated', plan_id: target.plan_id, migration_id: i % 2 ? target.id : null, actor: 'test', message: `n${i}`, data: {} });
    }
    const persisted = server.events;
    const tail = await client.get<Event[]>('/events', { query: { tail: true, limit: 3 } });
    expect(tail.map((e) => e.seq)).toEqual(persisted.slice(-3).map((e) => e.seq));
    const mine = await client.get<Event[]>('/events', { query: { tail: true, limit: 2, migration_id: target.id } });
    expect(mine.map((e) => e.message)).toEqual(['n1197', 'n1199']);
    // since still bounds the tail; without tail the first page is unchanged
    await expect(client.get<Event[]>('/events', { query: { tail: true, since: persisted.at(-1)?.seq } })).resolves.toEqual([]);
    const first = await client.get<Event[]>('/events', { query: { limit: 2 } });
    expect(first.map((e) => e.seq)).toEqual(persisted.slice(0, 2).map((e) => e.seq));
  });

  it('honours limit and offset on the migrations list', async () => {
    const { server, client } = setup();
    const all = await client.get<Migration[]>('/migrations');
    expect(all).toHaveLength(server.migrations.length);
    const page = await client.get<Migration[]>('/migrations', { query: { limit: 2, offset: 1 } });
    expect(page.map((m) => m.id)).toEqual(all.slice(1, 3).map((m) => m.id));
  });

  it('manages providers: PATCH, write-only credentials and conversion key (SDD §12)', async () => {
    const { server, client } = setup();
    const patched = await client.patch<Provider>('/providers/community-lab', { region: 'R2', distribution: 'kolla' });
    expect(patched).toMatchObject({ region: 'R2', distribution: 'kolla', status: 'unknown' });
    await expect(client.patch('/providers/community-lab', { kind: 'vmware' })).rejects.toMatchObject({ status: 422 });
    await expect(client.patch('/providers/community-lab', { distribution: 'vmware' })).rejects.toMatchObject({ status: 422 });
    await expect(setup('operator').client.patch('/providers/community-lab', { region: 'x' })).rejects.toMatchObject({ status: 403 });
    const stored = await client.put<Provider>('/providers/community-lab/credentials', { username: 'u', password: 'p', project_name: 'x' });
    expect(stored.credentials_secret).toBe('provider-community-lab');
    expect(JSON.stringify(stored)).not.toContain('"p"');
    await expect(client.put('/providers/vcenter-hq/credentials', { username: 'u', password: 'p', project_name: 'x' })).rejects.toMatchObject({ status: 422 });
    await expect(client.put('/providers/community-lab/conversion-key', { private_key: 'ssh-rsa AAAA' })).rejects.toMatchObject({ status: 422 });
    const keyed = await client.put<Provider>('/providers/community-lab/conversion-key', {
      private_key: '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----',
    });
    expect(keyed.conversion_host?.ssh_key_secret).toBe('provider-community-lab-ssh');
    expect(server.events.filter((e) => e.kind === 'provider.credentials_updated')).toHaveLength(2);
  });

  it('filters and pages the plans list', async () => {
    const { server, client } = setup();
    const all = await client.get<Plan[]>('/plans');
    expect(all).toHaveLength(server.plans.length);
    const running = await client.get<Plan[]>('/plans', { query: { status: 'running' } });
    expect(running.length).toBeGreaterThan(0);
    expect(running.every((p) => p.status === 'running')).toBe(true);
    const page = await client.get<Plan[]>('/plans', { query: { limit: 1, offset: 1 } });
    expect(page.map((p) => p.id)).toEqual(all.slice(1, 2).map((p) => p.id));
  });

  it('enforces route roles (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const waiting = byPhase(server, 'awaiting_cutover');
    await expect(client.post(`/migrations/${waiting.id}/approve`, {})).rejects.toMatchObject({ status: 403, code: 'forbidden' });
  });

  it('rejects transitions the FSM does not allow (SDD §5.1)', async () => {
    const { server, client } = setup('operator');
    const syncing = byPhase(server, 'syncing');
    await expect(client.post(`/migrations/${syncing.id}/retry`)).rejects.toMatchObject({ status: 409 });
    const failedAfterStop = byPhase(server, 'failed');
    expect(failedAfterStop.downtime_started_at).not.toBeNull();
    await expect(client.post(`/migrations/${failedAfterStop.id}/cancel`, {})).rejects.toMatchObject({ status: 409 });
  });

  it('cleans up after a cancel that leaves a data path, like the API (SDD §5.1)', async () => {
    const { server, client } = setup('operator');
    const actions = (id: string) =>
      server.events.filter((e) => e.migration_id === id && e.kind === 'migration.action').map((e) => e.data.action);
    const converged = server.migrations.find((m) => m.vm.name === 'app-billing-01')!;
    expect(converged.phase).toBe('awaiting_cutover');
    expect(converged.sync_passes.length).toBeGreaterThan(0);
    await client.post(`/migrations/${converged.id}/cancel`, {});
    expect(actions(converged.id).slice(-2)).toEqual(['cancel', 'cleanup']);
    expect(server.events.at(-1)?.message).toBe('app-billing-01: temporary resources of the cancelled migration removed');

    // a first pass still copying has no recorded pass yet: its copied data is cleaned up too
    const copying = server.migrations.find((m) => m.vm.name === 'db-pg-01')!;
    expect(copying.phase).toBe('precopy');
    expect(copying.sync_passes).toHaveLength(0);
    await client.post(`/migrations/${copying.id}/cancel`, {});
    expect(actions(copying.id).slice(-2)).toEqual(['cancel', 'cleanup']);

    // nothing copied yet: nothing to clean up
    const cold = server.migrations.find((m) => m.vm.name === 'ad-dc-01')!;
    expect(cold.phase).toBe('ready');
    expect(cold.sync_passes).toHaveLength(0);
    await client.post(`/migrations/${cold.id}/cancel`, {});
    expect(actions(cold.id).at(-1)).toBe('cancel');
    expect(actions(cold.id)).not.toContain('cleanup');
  });

  it('cancels a rolled-back migration and cleans up the data path its rollback kept, like the API (SDD §5.1)', async () => {
    const { server, client } = setup('operator');
    const actions = (id: string) =>
      server.events.filter((e) => e.migration_id === id && e.kind === 'migration.action').map((e) => e.data.action);
    const rolled = byPhase(server, 'rolled_back');
    // a warm migration whose rollback kept its destination volumes and warm state for a retry
    rolled.strategy = 'warm';
    rolled.sync_passes = server.migrations.find((m) => m.vm.name === 'app-billing-01')!.sync_passes.map((p) => ({ ...p }));
    const cancelled = await client.post<Migration>(`/migrations/${rolled.id}/cancel`, { reason: 'not migrated after all' });
    expect(cancelled.phase).toBe('cancelled');
    expect(actions(rolled.id).slice(-2)).toEqual(['cancel', 'cleanup']);
  });

  it('reopens a completed plan at a retry, and refuses one whose provider is gone, like the API (SDD §5.1)', async () => {
    const { server, client } = setup('operator');
    const rolled = byPhase(server, 'rolled_back');
    const plan = server.plans.find((p) => p.id === rolled.plan_id)!;
    plan.status = 'completed';
    const retried = await client.post<Migration>(`/migrations/${rolled.id}/retry`);
    expect(retried.phase).toBe('ready');
    expect(plan.status).toBe('running');
    const reopened = server.events.filter((e) => e.kind === 'plan.updated' && e.plan_id === plan.id);
    expect(reopened.at(-1)).toMatchObject({ actor: expect.any(String), data: { status: 'running' }, message: expect.stringContaining(`retry of ${rolled.vm.name}`) });

    // a completed plan's providers may be deleted (SDD §12): the reopened plan would need them
    const again = setup('operator');
    const gone = byPhase(again.server, 'rolled_back');
    const completed = again.server.plans.find((p) => p.id === gone.plan_id)!;
    completed.status = 'completed';
    const destination = again.server.providers.findIndex((p) => p.id === completed.destination_provider_id);
    again.server.providers.splice(destination, 1);
    await expect(again.client.post(`/migrations/${gone.id}/retry`)).rejects.toMatchObject({
      status: 409,
      message: expect.stringContaining(`provider '${completed.destination_provider_id}' no longer exists`),
    });
    expect(completed.status).toBe('completed');
    expect(again.server.migrations.find((m) => m.id === gone.id)?.phase).toBe('rolled_back');
  });

  it.each(['draft', 'validated', 'running', 'paused', 'failed'] as const)('leaves a %s plan as it is at a retry, like the API (SDD §5.1)', async (status) => {
    const { server, client } = setup('operator');
    const rolled = byPhase(server, 'rolled_back');
    const plan = server.plans.find((p) => p.id === rolled.plan_id)!;
    plan.status = status;
    const before = server.events.length;
    await client.post(`/migrations/${rolled.id}/retry`);
    expect(plan.status).toBe(status);
    expect(server.events.slice(before).filter((e) => e.kind === 'plan.updated')).toEqual([]);
  });

  it('refuses to start a waved plan while a migration belongs to no wave, like the API (SDD §9.4)', async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.status === 'validated' && p.waves.length > 0)!;
    const [added, replanned] = server.migrations.filter((m) => m.plan_id === plan.id && !['completed', 'finalized', 'cancelled', 'rolled_back'].includes(m.phase));
    added!.wave_id = null; // a VM added after the waves were planned
    replanned!.wave_id = 'wave-gone'; // its wave was planned away
    await expect(client.post(`/plans/${plan.id}/start`)).rejects.toMatchObject({
      status: 409,
      message: expect.stringContaining(`2 migration(s) belong to no wave: ${added!.vm.name}, ${replanned!.vm.name}`),
    });
    expect(plan.status).toBe('validated');
  });

  it('keeps a requested cutover waiting for a closed window unless an approver bypasses it, like the API (SDD §5.4)', async () => {
    const { server, client } = setup('approver');
    const m = server.migrations.find((x) => x.vm.name === 'ad-dc-01')!; // cold, ready, in a running plan
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.require_approval = false;
    plan.auto_cutover = false;
    plan.cutover_window = { start: new Date(NOW + 3_600_000).toISOString(), end: new Date(NOW + 7_200_000).toISOString() };
    await client.post(`/migrations/${m.id}/cutover`, {});
    server.tick();
    expect(m.phase).toBe('ready'); // requested, but its window opens in an hour
    await client.post(`/migrations/${m.id}/cutover`, { force_window: true });
    server.tick();
    expect(m.phase).toBe('cutover');
  });

  it("records a strategy change as the plan's override, so a re-validation keeps it, like the API (SDD §5.4)", async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.status === 'validated')!;
    const m = server.migrations.find((x) => x.plan_id === plan.id && x.phase === 'ready' && x.estimates.filter((e) => e.eligible).length >= 2)!;
    const chosen = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    await client.put(`/migrations/${m.id}/strategy`, { strategy: chosen });
    expect(plan.strategy_overrides[m.vm.source_id]).toBe(chosen);
    expect(server.events.filter((e) => e.kind === 'plan.updated').at(-1)).toMatchObject({
      plan_id: plan.id,
      data: { strategy_overrides: { [m.vm.source_id]: chosen } },
    });
    await client.post(`/plans/${plan.id}/validate`);
    expect(server.migrations.find((x) => x.id === m.id)!.strategy).toBe(chosen);
  });

  it('keeps the downtime clock of a retried cutover and never cancels a stopped source (SDD §5.1/§5.2)', async () => {
    const { server, client } = setup('operator');
    const failed = byPhase(server, 'failed');
    const stoppedAt = failed.downtime_started_at;
    expect(stoppedAt).not.toBeNull();
    expect(failed.downtime_ended_at).toBeNull();
    await expect(client.post(`/migrations/${failed.id}/cancel`, {})).rejects.toMatchObject({ status: 409, message: expect.stringMatching(/source VM is stopped/) });

    const retried = await client.post<Migration>(`/migrations/${failed.id}/retry`);
    expect(retried.phase).toBe('ready');
    expect(retried.downtime_started_at).toBe(stoppedAt);
    await expect(client.post(`/migrations/${failed.id}/cancel`, {})).rejects.toMatchObject({ status: 409, message: expect.stringMatching(/source VM is stopped/) });

    // the next cutover counts from the first stop, and the clock does not start again (like the API)
    const started = () => server.events.filter((e) => e.migration_id === failed.id && e.kind === 'migration.downtime_started').length;
    const startedBefore = started();
    const plan = server.plans.find((p) => p.id === failed.plan_id)!;
    plan.status = 'running';
    plan.cutover_window = null;
    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    await approver.post(`/migrations/${failed.id}/cutover`, {});
    await secondApprover(server, failed.id); // maker-checker (ADV-17): the requester's approval does not count
    server.tick();
    const again = server.migrations.find((m) => m.id === failed.id)!;
    expect(again.phase).toBe('cutover');
    expect(again.downtime_started_at).toBe(stoppedAt);
    expect(started()).toBe(startedBefore);
  });

  it('clears what the earlier attempt found about itself at a retry, and keeps an open clock with its model seconds, like the API (SDD §5.1, §5.2)', async () => {
    const { server, client } = setup('operator');
    const failed = byPhase(server, 'failed');
    // the failed fixture stopped its source: its clock is open
    expect([failed.downtime_started_at !== null, failed.downtime_ended_at]).toEqual([true, null]);
    Object.assign(failed, {
      data_issues: ['DOWNTIME_BELOW_FINAL_SYNC: 0 s of downtime around a 100 s final sync'],
      converged_by: 'slo',
      review_required: true,
      review_reason: 'application probe unreachable from the control plane: 10.0.0.5:80',
      checkpoint: 'cutover',
      downtime_model_s: 600,
    });
    const retried = await client.post<Migration>(`/migrations/${failed.id}/retry`);
    expect([retried.data_issues, retried.converged_by, retried.review_required, retried.review_reason, retried.checkpoint]).toEqual([[], null, false, null, null]);
    expect(retried.downtime_model_s).toBe(600);
  });

  it('starts no held migration, on a cutover request or a tick, like the API (SDD §5.4, §14.6)', async () => {
    const { server, client } = setup('approver');
    const m = byPhase(server, 'ready');
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    Object.assign(plan, { status: 'running', require_approval: false, auto_cutover: false, cutover_window: null, waves: [] });
    Object.assign(m, { strategy: 'cold', wave_id: null, advisor_hold: 'rec-held-0001' });
    await client.post(`/migrations/${m.id}/cutover`, {});
    server.tick();
    expect(m.phase).toBe('ready');
    m.advisor_hold = null; // a person decided
    server.tick();
    expect(m.phase).toBe('cutover');
  });

  /** Two ready warm migrations of one running plan, in the waves given (ids no fixture migration carries). */
  function waved(waves: (a: Migration, b: Migration) => Wave[]) {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const plan = server.plans.find((p) => server.migrations.filter((m) => m.plan_id === p.id && m.phase === 'ready').length >= 2)!;
    const [a, b] = server.migrations.filter((m) => m.plan_id === plan.id && m.phase === 'ready') as [Migration, Migration];
    Object.assign(plan, { status: 'running', require_approval: false, auto_cutover: true, cutover_window: null, waves: waves(a, b) });
    for (const m of [a, b]) Object.assign(m, { strategy: 'warm', advisor_hold: null, wave_id: plan.waves.find((w) => w.vm_ids.includes(m.vm.source_id))!.id });
    return { server, a, b };
  }

  it('starts a migration only once the waves it depends on are complete, like the API (SDD §8)', () => {
    const { server, a, b } = waved((a, b) => [
      { id: 'wave-t1', name: 'Pilot', order: 1, vm_ids: [a.vm.source_id], depends_on: [], max_parallel: 5 },
      { id: 'wave-t2', name: 'Wave 2', order: 2, vm_ids: [b.vm.source_id], depends_on: ['wave-t1'], max_parallel: 5 },
    ]);
    server.tick();
    expect([a.phase, b.phase]).toEqual(['precopy', 'ready']);
    a.phase = 'completed'; // the pilot wave is complete
    server.tick();
    expect(b.phase).toBe('precopy');
    // a retry in the pilot makes it incomplete again: a migration already waiting for its cutover waits too, as the
    // API ticks only active waves
    Object.assign(a, { phase: 'ready', advisor_hold: 'rec-held-0002' });
    b.phase = 'awaiting_cutover';
    server.tick();
    expect(b.phase).toBe('awaiting_cutover');
    a.phase = 'completed';
    server.tick();
    expect(b.phase).toBe('cutover');
  });

  it("starts no more of a wave's migrations than its max_parallel, like the API (SDD §8)", () => {
    const { server, a, b } = waved((a, b) => [
      { id: 'wave-t1', name: 'Pilot', order: 1, vm_ids: [a.vm.source_id, b.vm.source_id], depends_on: [], max_parallel: 1 },
    ]);
    server.tick();
    expect([a.phase, b.phase].sort()).toEqual(['precopy', 'ready']);
  });

  it('records the strategy of the attempt a retry retries, oldest first, like the API (SDD §4.2, §14.4)', async () => {
    const { server, client } = setup('operator');
    const failed = byPhase(server, 'failed');
    expect([failed.strategy, failed.attempts]).toEqual(['cold', 1]);
    failed.attempt_strategies = ['warm']; // its first attempt ran warm
    const retried = await client.post<Migration>(`/migrations/${failed.id}/retry`);
    expect([retried.attempts, retried.attempt_strategies]).toEqual([2, ['warm', 'cold']]);
  });

  it('counts bytes over every pass while migrations run and reports the running step in progress events, like the API (SDD §4.2, §4.3)', () => {
    const { server } = setup('operator');
    for (const plan of server.plans) plan.cutover_window = null;
    const progress: Event[] = [];
    const syncPasses: Event[] = [];
    // the running step's bytes, as the page learns them: a progress event's bytes_done until its pass ends
    const stepBytes = new Map<string, number>();
    server.subscribe((e) => {
      if (e.kind === 'migration.progress') {
        progress.push(e);
        stepBytes.set(e.migration_id!, e.data.bytes_done as number);
      }
      if (e.kind === 'migration.sync_pass') {
        syncPasses.push(e);
        stepBytes.set(e.migration_id!, 0);
      }
    });
    const ended = (m: Migration) => m.sync_bytes_dropped + m.sync_passes.reduce((sum, p) => sum + (p.ended_at === null ? 0 : p.bytes_transferred), 0);
    // while a migration moves forward, the figure never drops (a pass has one size, drawn once)
    const forward = new Set<Migration['phase']>(['precopy', 'syncing', 'awaiting_cutover', 'cutover', 'verifying']);
    const seen = new Map(server.migrations.map((m) => [m.id, { phase: m.phase, bytes: m.bytes_transferred }]));
    for (let t = 0; t < 80; t++) {
      server.tick();
      for (const m of server.migrations) {
        const before = seen.get(m.id)!;
        if (forward.has(before.phase) && forward.has(m.phase)) expect(m.bytes_transferred, m.id).toBeGreaterThanOrEqual(before.bytes);
        seen.set(m.id, { phase: m.phase, bytes: m.bytes_transferred });
        expect(m.bytes_total, m.id).toBe(m.vm.used_bytes);
        // like the API, the running pass is listed only once it ends (SDD §4.2)
        expect(m.sync_passes.filter((p) => p.ended_at === null), m.id).toEqual([]);
        if (stepBytes.has(m.id)) expect(m.bytes_transferred, m.id).toBe(ended(m) + stepBytes.get(m.id)!);
      }
    }
    expect(progress.length).toBeGreaterThan(0);
    for (const e of progress) {
      expect(Object.keys(e.data).sort()).toEqual(['bytes_done', 'bytes_total', 'pct', 'phase']);
      expect(e.data.bytes_done as number).toBeLessThanOrEqual(e.data.bytes_total as number);
    }
    // the sync_pass event's data is the pass that ended, as the API sends it (SDD §4.3)
    expect(syncPasses.length).toBeGreaterThan(0);
    for (const e of syncPasses) expect(e.data).toMatchObject({ number: expect.any(Number), ended_at: expect.any(String), bytes_transferred: expect.any(Number) });
    // a migration the fixtures start mid-cutover finishes its final pass with the bytes it moved
    const midCutover = server.migrations.find((m) => m.id === 'mig-3c1a0f9e23')!;
    expect(midCutover.phase).toBe('verifying');
    expect(midCutover.sync_passes.at(-1)).toMatchObject({ kind: 'final', ended_at: expect.any(String) });
    expect(midCutover.sync_passes.at(-1)!.bytes_transferred).toBeGreaterThan(0);
  });

  it('records nothing for a pass a rollback interrupts, so a retried migration runs a new pass, like the API (SDD §4.2)', async () => {
    let clock = NOW;
    const server = new MockServer({ now: () => clock, seed: 1 });
    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    // a warm migration the fixtures start mid-cutover: its final pass runs
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e23')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    server.tick();
    const listed = m.sync_passes.map((p) => p.number);
    await approver.post(`/migrations/${m.id}/rollback`, { reason: 'interrupt the final pass' });
    for (let t = 0; t < 50 && m.phase !== 'rolled_back'; t++) server.tick();
    expect(m.phase).toBe('rolled_back');
    expect(m.sync_passes.map((p) => p.number)).toEqual(listed);

    await approver.post(`/migrations/${m.id}/retry`, {});
    // the plan starts again (its blocked VM set aside)
    for (const x of server.migrations) if (x.plan_id === plan.id && x.phase === 'blocked') x.phase = 'cancelled';
    plan.status = 'paused';
    clock += 3_600_000;
    const restarted = Date.parse(new Date(clock).toISOString());
    await approver.post(`/plans/${plan.id}/start`, {});
    expect(m.phase).toBe('precopy');
    for (let t = 0; t < 300 && m.sync_passes.length === listed.length; t++) server.tick();
    // the first pass recorded after the restart is a new one, not the interrupted final pass
    expect(m.sync_passes.length).toBe(listed.length + 1);
    expect(m.sync_passes.at(-1)!.kind).not.toBe('final');
    expect(Date.parse(m.sync_passes.at(-1)!.started_at)).toBeGreaterThanOrEqual(restarted);
  });

  it('empties what the passes said the cutover would refuse once the cutover gets past it, as the control plane does (SDD §7.2)', async () => {
    const { server } = setup('approver');
    const m = server.migrations.find((x) => x.strategy === 'cold' && x.phase === 'ready')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.status = 'running';
    plan.cutover_window = null;
    m.cutover_refusals = ["port p2 on network 'gone' does not resolve"];
    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    await approver.post(`/migrations/${m.id}/cutover`, {});
    await secondApprover(server, m.id); // maker-checker (ADV-17)
    for (let t = 0; t < 200 && m.phase !== 'verifying'; t++) server.tick();
    expect(m.phase).toBe('verifying');
    expect(m.cutover_refusals).toEqual([]);
  });

  it('records a cold copy as a full pass and keeps its bytes, as the API executors do (SDD §4.2)', async () => {
    const { server } = setup('approver');
    const m = server.migrations.find((x) => x.strategy === 'cold' && x.phase === 'ready')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.status = 'running';
    plan.cutover_window = null;
    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    await approver.post(`/migrations/${m.id}/cutover`, {});
    await secondApprover(server, m.id); // maker-checker (ADV-17)
    for (let t = 0; t < 200 && m.phase !== 'verifying'; t++) server.tick();
    expect(m.phase).toBe('verifying');
    expect(m.sync_passes.at(-1)).toMatchObject({ kind: 'full', bytes_transferred: m.vm.used_bytes });
    expect(m.bytes_transferred).toBe(m.vm.used_bytes);
    expect(m.bytes_total).toBe(m.vm.used_bytes);
  });

  it('keeps the first max_sync_passes and the latest 20 sync passes of a long wait, like the API (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const m = byPhase(server, 'awaiting_cutover');
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    // the cutover gate stays closed, so every requested sync is one more keep-warm pass
    plan.auto_cutover = false;
    m.cutover_requested = false;
    const transferred = new Map<number, number>();
    const record = () => m.sync_passes.forEach((p) => transferred.set(p.number, p.bytes_transferred));
    record();
    for (let i = 0; i < plan.max_sync_passes + 25; i++) {
      await client.post(`/migrations/${m.id}/sync`, {});
      for (let t = 0; t < 200 && m.phase !== 'awaiting_cutover'; t++) server.tick();
      expect(m.phase).toBe('awaiting_cutover');
      record();
    }

    // pass numbers keep counting; the list holds the first max_sync_passes and the latest 20
    const last = transferred.size;
    expect([...transferred.keys()]).toEqual(Array.from({ length: last }, (_, i) => i + 1));
    const kept = [...Array.from({ length: plan.max_sync_passes }, (_, i) => i + 1), ...Array.from({ length: 20 }, (_, i) => last - 19 + i)];
    expect(m.sync_passes.map((p) => p.number)).toEqual(kept);
    // the dropped passes' bytes stay counted, on the migration and in the stats
    const dropped = [...transferred].filter(([n]) => !kept.includes(n)).reduce((sum, [, b]) => sum + b, 0);
    expect(dropped).toBeGreaterThan(0);
    expect(m.sync_bytes_dropped).toBe(dropped);
    const others = server.migrations
      .filter((x) => x.id !== m.id)
      .reduce((sum, x) => sum + x.sync_bytes_dropped + x.sync_passes.reduce((s, p) => s + p.bytes_transferred, 0) + (x.sync_passes.length ? 0 : x.bytes_transferred), 0);
    const all = [...transferred.values()].reduce((sum, b) => sum + b, 0);
    const stats = await client.get<{ bytes_transferred: number }>('/stats');
    expect(stats.bytes_transferred).toBe(others + all);

    // the final pass of the cutover is kept the same way
    plan.status = 'running';
    plan.cutover_window = null;
    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    await approver.post(`/migrations/${m.id}/cutover`, {});
    await secondApprover(server, m.id); // maker-checker (ADV-17)
    for (let t = 0; t < 200 && m.phase !== 'verifying'; t++) server.tick();
    expect(m.phase).toBe('verifying');
    expect(m.sync_passes.map((p) => p.number)).toEqual([...kept.slice(0, plan.max_sync_passes), ...kept.slice(plan.max_sync_passes + 1), last + 1]);
    expect(m.sync_passes.at(-1)?.kind).toBe('final');
    expect(m.sync_bytes_dropped).toBe(dropped + transferred.get(last - 19)!);
  });

  it('refuses to edit or re-plan the waves while a migration is in flight (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const waiting = server.migrations.find((x) => x.plan_id === plan.id)!;
    waiting.phase = 'awaiting_cutover';
    await expect(client.patch(`/plans/${plan.id}`, { description: 'move' })).rejects.toMatchObject({ status: 409, message: expect.stringContaining(waiting.vm.name) });
    await expect(client.post(`/plans/${plan.id}/waves/auto`, { max_wave_size: 5 })).rejects.toMatchObject({ status: 409 });
    // each VM with what releases it, in the API's words: a completed one is finalized or rolled back
    waiting.phase = 'completed';
    const finalize = `finalize or roll back the completed migration (${waiting.vm.name})`;
    await expect(client.patch(`/plans/${plan.id}`, { description: 'move' })).rejects.toMatchObject({ status: 409, message: `${finalize} before editing the plan` });
    await expect(client.post(`/plans/${plan.id}/waves/auto`, { max_wave_size: 5 })).rejects.toMatchObject({
      status: 409,
      message: `${finalize} before re-planning the waves`,
    });
  });

  it('lets only an approver change the approval policy of a plan (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const base = { name: 'Policy', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: ['os-0a11'] };
    // an operator may spell out the defaults…
    const created = await client.post<Plan>('/plans', { ...base, require_approval: true, auto_cutover: false, cutover_window: null });
    expect(created.status).toBe('draft');
    // …but not change them, and the refusal comes before any other check (QASuite S-01)
    await expect(client.post('/plans', { ...base, destination_provider_id: 'nope', auto_cutover: true })).rejects.toMatchObject({
      status: 403,
      code: 'forbidden',
      message: expect.stringContaining('auto_cutover'),
    });

    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const window = { start: '2026-10-10T20:00:00Z', end: '2026-10-11T02:00:00Z' };
    // re-sending the current values changes nothing
    await client.patch(`/plans/${plan.id}`, { require_approval: plan.require_approval, cutover_window: plan.cutover_window });
    await expect(client.patch(`/plans/${plan.id}`, { require_approval: !plan.require_approval })).rejects.toMatchObject({
      status: 403,
      message: expect.stringContaining('require_approval'),
    });
    await expect(client.patch(`/plans/${plan.id}`, { cutover_window: window, description: 'night' })).rejects.toMatchObject({ status: 403 });
    expect(plan.description).not.toBe('night');

    const approver = new ApiClient({ getToken: () => 'approver', fetchImpl: createMockFetch(server) });
    const changed = await approver.patch<Plan>(`/plans/${plan.id}`, { require_approval: false, cutover_window: window });
    expect(changed).toMatchObject({ require_approval: false, cutover_window: window });
  });

  it('creates a plan\'s migrations at validation, not at creation, like the API (SDD §8)', async () => {
    const { server, client } = setup('operator');
    const free = freeVms(server, 'rhosp17-dc1', 2);
    const plan = await client.post<Plan>('/plans', {
      name: 'Fresh',
      source_provider_id: 'rhosp17-dc1',
      destination_provider_id: 'rhoso-prod',
      vm_ids: free.map((v) => v.source_id),
    });
    expect(server.migrations.filter((m) => m.plan_id === plan.id)).toEqual([]);

    const report = await client.post<{ migrations: { vm_name: string; phase: string }[] }>(`/plans/${plan.id}/validate`);
    expect(report.migrations.map((m) => m.vm_name).sort()).toEqual(free.map((v) => v.name).sort());
    expect(report.migrations.every((m) => m.phase === 'ready' || m.phase === 'blocked')).toBe(true);
    const ids = server.migrations.filter((m) => m.plan_id === plan.id).map((m) => m.id).sort();
    expect(ids).toHaveLength(2);
    // validating again keeps the same migrations
    await client.post(`/plans/${plan.id}/validate`);
    expect(server.migrations.filter((m) => m.plan_id === plan.id).map((m) => m.id).sort()).toEqual(ids);
  });

  it("blocks a NIC on another project's network that the RHOSO token may not put a port on, judged with its roles like the API (SDD §9.3, §10)", async () => {
    const { server, client } = setup('operator');
    const [vm] = freeVms(server, 'rhosp17-dc1', 1);
    // the VM moved to the shared provider-ext, an address of its subnet
    vm!.nics = [{ ...vm!.nics[0]!, network: 'provider-ext', fixed_ips: ['203.0.113.77'] }];
    const plan = await client.post<Plan>('/plans', {
      name: 'Shared network',
      source_provider_id: 'rhosp17-dc1',
      destination_provider_id: 'rhoso-prod',
      vm_ids: [vm!.source_id],
    });
    const codes = async () => {
      await client.post(`/plans/${plan.id}/validate`);
      return server.migrations.find((m) => m.plan_id === plan.id)!.findings.map((f) => f.code);
    };
    // the mock's admin token may
    expect(await codes()).not.toContain('NET_PORT_NOT_PERMITTED');
    const destination = server.providers.find((p) => p.id === 'rhoso-prod')!;
    destination.capabilities = { ...destination.capabilities, roles: ['member', 'reader'] };
    expect(await codes()).toContain('NET_PORT_NOT_PERMITTED');
    expect(server.migrations.find((m) => m.plan_id === plan.id)!.phase).toBe('blocked');
  });

  it('cancels the migration of a VM taken out of a plan at the next validation, and refuses it back (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const [keep, drop] = freeVms(server, 'rhosp17-dc1', 2);
    const plan = await client.post<Plan>('/plans', {
      name: 'Shrinking',
      source_provider_id: 'rhosp17-dc1',
      destination_provider_id: 'rhoso-prod',
      vm_ids: [keep!.source_id, drop!.source_id],
    });
    await client.post(`/plans/${plan.id}/validate`);
    const dropped = server.migrations.find((m) => m.plan_id === plan.id && m.vm.source_id === drop!.source_id)!;

    // a VM whose source is stopped cannot leave the plan (SDD §5.1): the API refuses that PATCH, so
    // the plan is changed the way `seamless plan apply` writes it, and validation refuses it
    dropped.downtime_started_at = '2026-10-08T11:00:00Z';
    server.plans.find((p) => p.id === plan.id)!.vm_ids = [keep!.source_id];
    await expect(client.post(`/plans/${plan.id}/validate`)).rejects.toMatchObject({ status: 409, message: expect.stringContaining('the source VM is stopped') });
    expect(dropped.phase).not.toBe('cancelled');

    dropped.downtime_started_at = null;
    await client.post(`/plans/${plan.id}/validate`);
    expect(dropped.phase).toBe('cancelled');
    // cancelled is terminal: the VM cannot come back into this plan
    await client.patch(`/plans/${plan.id}`, { vm_ids: [keep!.source_id, drop!.source_id] });
    await expect(client.post(`/plans/${plan.id}/validate`)).rejects.toMatchObject({
      status: 400,
      message: expect.stringContaining(`1 VM(s) have a cancelled migration: ${drop!.name}`),
    });
  });

  it('answers 502 for a VM the source no longer reports, before changing anything, like the API (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const [keep] = freeVms(server, 'rhosp17-dc1', 1);
    const plan = await client.post<Plan>('/plans', {
      name: 'Vanished',
      source_provider_id: 'rhosp17-dc1',
      destination_provider_id: 'rhoso-prod',
      vm_ids: [keep!.source_id, 'vm-gone-0001'],
    });
    await expect(client.post(`/plans/${plan.id}/validate`)).rejects.toMatchObject({
      status: 502,
      message: "VM 'vm-gone-0001': VM 'vm-gone-0001' not found",
    });
    expect(server.migrations.filter((m) => m.plan_id === plan.id)).toEqual([]);
    expect(server.plans.find((p) => p.id === plan.id)!.status).toBe('draft');
  });

  it('refuses a list page outside the bounds the API takes, with its words (SDD §12)', async () => {
    const { client } = setup('viewer');
    const refused = (path: string, message: string) => expect(client.get(path)).rejects.toMatchObject({ status: 422, message });
    await refused('/plans?limit=0', 'query.limit: Input should be greater than or equal to 1');
    await refused('/plans?limit=1001', 'query.limit: Input should be less than or equal to 1000');
    await refused('/plans?offset=-1', 'query.offset: Input should be greater than or equal to 0');
    await refused('/migrations?limit=5001', 'query.limit: Input should be less than or equal to 5000');
    await refused('/migrations?limit=two', 'query.limit: Input should be a valid integer, unable to parse string as an integer');
    await refused('/events?limit=1001', 'query.limit: Input should be less than or equal to 1000');
    await refused('/events?limit=0', 'query.limit: Input should be greater than or equal to 1');
    // in bounds, a page
    expect(await client.get<unknown[]>('/plans?limit=1000')).toEqual(expect.any(Array));
    expect((await client.get<unknown[]>('/migrations?limit=2&offset=1')).length).toBe(2);
    expect((await client.get<unknown[]>('/events?limit=3')).length).toBe(3);
  });

  it("checks a provider route's role before it looks the provider up, like the API (SDD §12, §13.2)", async () => {
    const as = (token: string) => setup(token).client;
    const refused = (call: Promise<unknown>, status: number) => expect(call).rejects.toMatchObject({ status });
    await refused(as('operator').delete('/providers/nope'), 403);
    await refused(as('operator').patch('/providers/nope', { name: 'x' }), 403);
    await refused(as('operator').put('/providers/nope/credentials', { username: 'u' }), 403);
    await refused(as('viewer').post('/providers/nope/check'), 403);
    // with the role, an unknown provider is 404
    await refused(as('admin').delete('/providers/nope'), 404);
    await refused(as('operator').post('/providers/nope/check'), 404);
    await refused(as('viewer').get('/providers/nope'), 404);
  });

  it('refuses an edit naming a field a plan cannot change, before anything else, like the API (SDD §12)', async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.status === 'draft' || p.status === 'validated')!;
    const before = JSON.stringify(plan);
    await expect(client.patch(`/plans/${plan.id}`, { description: 'x', status: 'running', waves: [], id: 'plan-00000000' })).rejects.toMatchObject({
      status: 422,
      message: 'fields cannot be changed: id, status, waves',
    });
    expect(JSON.stringify(plan)).toBe(before);
    // the API's PLAN_EDITABLE_FIELDS, field for field
    expect([...PLAN_EDITABLE_FIELDS].sort()).toEqual([
      'auto_cutover',
      'convergence_threshold_bytes',
      'cutover_window',
      'default_strategy',
      'description',
      'destination_provider_id',
      'downtime_slo_s',
      'estimator_overrides',
      'finalize_warning_h',
      'handover',
      'keep_warm_interval_s',
      'link_bps',
      'mappings',
      'max_sync_passes',
      'name',
      'nic_overrides',
      'port_creation_mode',
      'prestage_resources',
      'probe_overrides',
      'require_approval',
      'require_distinct_approver',
      'selection_policy',
      'source_provider_id',
      'strategy_overrides',
      'verification',
      'vm_ids',
    ]);
  });

  it('refuses the plans the API refuses on create and on edit, with its words (SDD §12)', async () => {
    const { client } = setup('approver');
    const base = { name: 'Checked', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: ['vm-b', 'vm-a'] };
    const refused = async (body: Record<string, unknown>, message: string) =>
      expect(client.post('/plans', { ...base, ...body })).rejects.toMatchObject({ status: 422, message });
    await refused({ vm_ids: ['vm-b', 'vm-a', 'vm-b', 'vm-a', 'vm-c'] }, 'vm_ids lists a VM more than once: vm-a, vm-b');
    await refused({ cutover_window: { start: '2026-10-08T14:00:00Z', end: '2026-10-08T12:00:00Z' } }, 'cutover_window: end must be after start');
    await refused({ verification: { tcp_ports: [22, 0, 70000] } }, 'verification.tcp_ports [0, 70000] outside 1-65535');
    await refused(
      { verification: { windows_tcp_ports: [3389, 65536], timeout_s: -5 } },
      'verification.windows_tcp_ports [65536] outside 1-65535; verification.timeout_s must not be negative (got -5)',
    );
    // a pre-stage name without an export/import playbook pair, or the workloads themselves (SDD §4.2)
    await refused({ prestage_resources: ['networks', 'flavours', 'workloads'] }, "prestage_resources: cannot pre-stage 'flavours', 'workloads'");
    await expect(client.post('/plans', { ...base, name: 'Flavors', prestage_resources: ['networks', 'flavors'] })).resolves.toMatchObject({ prestage_resources: ['networks', 'flavors'] });
    // an edit is checked on the plan it would store, kept fields included
    const plan = await client.post<Plan>('/plans', base);
    await expect(client.patch(`/plans/${plan.id}`, { cutover_window: { start: '2026-10-08T14:00:00Z', end: '2026-10-08T14:00:00Z' } })).rejects.toMatchObject({
      status: 422,
      message: 'cutover_window: end must be after start',
    });
    await expect(client.patch(`/plans/${plan.id}`, { vm_ids: ['vm-a', 'vm-a'] })).rejects.toMatchObject({ status: 422, message: 'vm_ids lists a VM more than once: vm-a' });
  });

  it('refuses a VM another plan holds, at validation and at a retry that would take it back (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const first = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const held = server.migrations.find((m) => m.plan_id === first.id && !['pending', 'cancelled', 'finalized', 'rolled_back'].includes(m.phase))!;
    const second = await client.post<Plan>('/plans', {
      name: 'Second wave',
      source_provider_id: first.source_provider_id,
      destination_provider_id: first.destination_provider_id,
      vm_ids: [held.vm.source_id],
    });
    await expect(client.post(`/plans/${second.id}/validate`)).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
      message: expect.stringContaining(`${held.vm.name} (plan "${first.name}", ${first.id}, ${held.phase})`),
    });
    // a rolled-back migration lets its VM go; its retry would take it back
    held.phase = 'rolled_back';
    const report = await client.post<{ migrations: { vm_name: string }[] }>(`/plans/${second.id}/validate`);
    expect(report.migrations.map((m) => m.vm_name)).toEqual([held.vm.name]);
    await expect(client.post(`/migrations/${held.id}/retry`)).rejects.toMatchObject({ status: 409, message: expect.stringContaining('Second wave') });
    expect(held.phase).toBe('rolled_back');
  });

  it('refuses to cancel a migration being validated, and cancels it once its VM left the plan, like the API (SDD §5.1, §5.4)', async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.status === 'draft')!;
    await client.post(`/plans/${plan.id}/validate`);
    const [kept, dropped] = server.migrations.filter((m) => m.plan_id === plan.id);
    // an interrupted validation left both in validating
    kept!.phase = 'validating';
    dropped!.phase = 'validating';
    await expect(client.post(`/migrations/${dropped!.id}/cancel`, {})).rejects.toMatchObject({
      status: 409,
      message: expect.stringContaining('validate the plan again'),
    });
    await client.patch(`/plans/${plan.id}`, { vm_ids: [kept!.vm.source_id] });
    await client.post(`/plans/${plan.id}/validate`);
    expect(dropped!.phase).toBe('cancelled');
    expect(dropped!.phase_history.at(-1)?.reason).toBe('removed from the plan');
    expect(kept!.phase).not.toBe('validating'); // taken over by the validation (SDD §5.4)
  });

  it('refuses a similar-incident search outside the bounds the API accepts (SDD §12)', async () => {
    const { client } = setup('operator');
    for (const body of [{ query: '' }, { query: 'x'.repeat(2001) }, { query: 'disk', limit: 0 }, { query: 'disk', limit: 21 }]) {
      await expect(client.post('/advisor/similar-incidents', body), JSON.stringify(body).slice(0, 40)).rejects.toMatchObject({ status: 422 });
    }
    await expect(client.post('/advisor/similar-incidents', { query: 'x'.repeat(2000), limit: 20 })).resolves.toMatchObject({ hits: expect.any(Array) });
  });

  it('lets a never-validated plan hold nothing, and a retry go ahead once the other plan lets the VM go (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const first = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const mine = server.migrations.find((m) => m.plan_id === first.id)!;
    const second = await client.post<Plan>('/plans', {
      name: 'Second wave',
      source_provider_id: first.source_provider_id,
      destination_provider_id: first.destination_provider_id,
      vm_ids: [mine.vm.source_id],
    });
    // the second plan has not been validated: like in the API it has no migration yet, so it holds nothing
    await client.post(`/plans/${first.id}/validate`);
    await expect(client.post(`/plans/${second.id}/validate`)).rejects.toMatchObject({ status: 409, message: expect.stringContaining(first.name) });

    // the first plan's migration rolls back and the second plan takes the VM; once the second
    // plan cancels its migration, the first plan's retry goes ahead
    mine.phase = 'rolled_back';
    await client.post(`/plans/${second.id}/validate`);
    const theirs = server.migrations.find((m) => m.plan_id === second.id)!;
    await expect(client.post(`/migrations/${mine.id}/retry`)).rejects.toMatchObject({ status: 409 });
    await client.post(`/migrations/${theirs.id}/cancel`, { reason: 'back to the first plan' });
    await client.post(`/migrations/${mine.id}/retry`);
    expect(mine.phase).toBe('ready');
  });

  it('clears approvals, the cutover request and force_window on re-validation and on a strategy change, like the API (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const m = server.migrations.find((x) => x.plan_id === plan.id && x.estimates.filter((e) => e.eligible).length >= 2)!;
    const approve = () => {
      m.phase = 'ready';
      m.approvals = [{ actor: 'sari', at: '2026-10-08T11:00:00Z', comment: null }];
      m.cutover_requested = true;
      m.force_window = true;
    };
    approve();
    await client.post(`/plans/${plan.id}/validate`);
    expect(m).toMatchObject({ approvals: [], cutover_requested: false, force_window: false });

    approve();
    const other = m.estimates.find((e) => e.eligible && e.strategy !== m.strategy)!.strategy;
    await client.put(`/migrations/${m.id}/strategy`, { strategy: other });
    expect(m).toMatchObject({ strategy: other, approvals: [], cutover_requested: false, force_window: false });
  });

  it('counts VMs, not migrations, and names each holder once in the cross-plan refusal (SDD §5.4)', async () => {
    const { server, client } = setup('operator');
    const first = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const mine = server.migrations.filter((m) => m.plan_id === first.id).slice(0, 2);
    for (const m of mine) m.phase = 'ready';
    // a plan from before the rule, with the same name, holds the same VMs again
    const legacy: Plan = { ...first, id: 'plan-legacy00' };
    server.plans.push(legacy);
    for (const m of mine) server.migrations.push({ ...m, id: `${m.id}-legacy`, plan_id: legacy.id });
    const third = await client.post<Plan>('/plans', {
      name: 'Third wave',
      source_provider_id: first.source_provider_id,
      destination_provider_id: first.destination_provider_id,
      vm_ids: mine.map((m) => m.vm.source_id),
    });
    const [a, b] = mine.map((m) => m.vm.name).sort();
    // the two plans share a name: each is named by its id too (SDD §5.4)
    const [p, q] = [first.id, legacy.id].sort();
    const named = (vm: string) => `${vm} (plan "${first.name}", ${p}, ready), ${vm} (plan "${first.name}", ${q}, ready)`;
    await expect(client.post(`/plans/${third.id}/validate`)).rejects.toMatchObject({
      status: 409,
      message: `2 VM(s) already have a migration in another plan: ${named(a!)}, ${named(b!)}; finish, roll back or cancel it there, or remove the VM from vm_ids`,
    });
  });

  it('starts a failed plan again, like the API (SDD §8, §12)', async () => {
    const { server, client } = setup('operator');
    const failed = server.plans.find((p) => p.status === 'failed')!;
    const started = await client.post<Plan>(`/plans/${failed.id}/start`);
    expect(started.status).toBe('running');
  });

  it('finalizes only with the typed VM name', async () => {
    const { server, client } = setup('approver');
    const completed = byPhase(server, 'completed');
    await expect(
      client.post(`/migrations/${completed.id}/finalize`, { confirm: 'wrong', delete_source: false }),
    ).rejects.toMatchObject({ status: 400, code: 'bad_request' });
    const result = await client.post<Migration>(`/migrations/${completed.id}/finalize`, { confirm: completed.vm.name });
    expect(result.phase).toBe('finalized');
    expect(result.checkpoint).toBe('finalized'); // the API's value (SDD §8.1)
    // like the API (SDD §12): the phase is checked first - a migration that is not completed answers 409, whatever its name
    const ready = byPhase(server, 'ready');
    await expect(client.post(`/migrations/${ready.id}/finalize`, { confirm: 'wrong' })).rejects.toMatchObject({
      status: 409,
      message: 'only completed migrations can be finalized (ready)',
    });
  });

  it('refuses to start a plan with blocked migrations', async () => {
    const { server, client } = setup('operator');
    const blocked = byPhase(server, 'blocked');
    const plan = server.plans.find((p) => p.id === blocked.plan_id);
    if (!plan) throw new Error('plan missing');
    plan.status = 'validated';
    await expect(client.post(`/plans/${plan.id}/start`)).rejects.toMatchObject({ status: 409, code: 'conflict' });
  });

  it('serves events over SSE to the real stream reader', async () => {
    const { server, client } = setup('viewer');
    const controller = new AbortController();
    const received: Event[] = [];
    const done = streamEvents(
      null,
      (event) => {
        received.push(event);
        controller.abort();
      },
      controller.signal,
      {
        client,
        onStatus: (status) => {
          if (status === 'open') {
            server.emit({ kind: 'plan.paused', plan_id: 'plan-x', migration_id: null, actor: 'bayu', message: 'paused', data: {} });
          }
        },
      },
    );
    await done;
    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({ kind: 'plan.paused', actor: 'bayu' });
    expect(received[0]?.seq).toBe(server.events.at(-1)?.seq);
  });

  it('returns stats with 60 throughput buckets', async () => {
    const { client } = setup('viewer');
    const stats = await client.get<{ throughput_series: unknown[]; total: number }>('/stats');
    expect(stats.throughput_series).toHaveLength(60);
    expect(stats.total).toBeGreaterThan(20);
  });

  it('plans waves with any size from 1 to 1000 and refuses others like the API (SDD §12)', () => {
    const { server } = setup('operator');
    const path = '/plans/plan-0e9f6a17/waves/auto';
    for (const [size, bound] of [
      [0, 'greater than or equal to 1'],
      [1001, 'less than or equal to 1000'],
    ] as const) {
      const refused = server.handle('POST', path, new URLSearchParams(), { max_wave_size: size }, 'operator');
      expect(refused.status).toBe(422);
      expect(refused.body).toEqual({ error: { code: 'validation_error', message: `max_wave_size: Input should be ${bound}` } });
    }
    expect(server.handle('POST', path, new URLSearchParams(), { max_wave_size: 1000 }, 'operator').status).toBe(200);
  });

  it('retries like the API: clears the cutover request and its window bypass, keeps approvals (SDD §5.1)', () => {
    const { server } = setup('operator');
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    m.phase = 'failed';
    m.approvals = [{ actor: 'ana', at: '2026-10-08T11:00:00Z', comment: null }];
    m.cutover_requested = true;
    m.force_window = true;
    const result = server.handle('POST', `/migrations/${m.id}/retry`, new URLSearchParams(), {}, 'operator');
    expect(result.status).toBe(200);
    expect(m).toMatchObject({ phase: 'ready', cutover_requested: false, force_window: false });
    expect(m.approvals.map((a) => a.actor)).toEqual(['ana']);
  });

  it('refuses to cancel a plan while an unstarted migration has its source stopped, before cancelling anything, like the API (SDD §12)', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const mine = server.migrations.filter((m) => m.plan_id === 'plan-c81d44a0').sort((a, b) => (a.id < b.id ? -1 : 1));
    expect(mine.length).toBeGreaterThan(1);
    // the last one in id order: a cancel loop would reach the others first
    const last = mine.at(-1)!;
    Object.assign(last, { phase: 'ready', downtime_started_at: '2026-10-08T10:00:00Z', downtime_ended_at: null });
    const before = mine.map((m) => m.phase);
    const res = server.handle('POST', '/plans/plan-c81d44a0/cancel', new URLSearchParams(), {}, 'operator');
    expect(res.status).toBe(409);
    expect((res.body as { error: { message: string } }).error.message).toMatch(new RegExp(`${last.vm.name}; cut it over`));
    expect(mine.map((m) => m.phase)).toEqual(before);
  });

  it('cancels a plan like the API: its unstarted migrations cancelled, a running plan paused first, a started migration named (SDD §12)', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const call = (id: string, token = 'operator') => server.handle('POST', `/plans/${id}/cancel`, new URLSearchParams(), { reason: 'made by mistake' }, token);
    expect(call('plan-c81d44a0', 'viewer').status).toBe(403);
    expect(call('plan-4f2a9c1e')).toMatchObject({ status: 409, body: { error: { message: 'pause the plan before cancelling it' } } });
    const started = call('plan-7b3e0d52');
    expect(started.status).toBe(409);
    expect((started.body as { error: { message: string } }).error.message).toMatch(/^only a plan whose migrations have not started can be cancelled: .+ \(syncing\)/);
    const before = server.events.length;
    const done = call('plan-c81d44a0');
    expect(done.status).toBe(200);
    expect(done.body).toMatchObject({ status: 'cancelled', derived_status: 'cancelled' });
    expect(new Set(server.migrations.filter((m) => m.plan_id === 'plan-c81d44a0').map((m) => m.phase))).toEqual(new Set(['cancelled']));
    const events = server.events.slice(before);
    const n = server.migrations.filter((m) => m.plan_id === 'plan-c81d44a0').length;
    const operator = (server.handle('GET', '/me', new URLSearchParams(), undefined, 'operator').body as { name: string }).name;
    expect(events.filter((e) => e.kind === 'plan.cancelled')).toMatchObject([{ plan_id: 'plan-c81d44a0', actor: operator, data: { reason: 'made by mistake', migrations: n } }]);
    // terminal: no validation, start or edit, and a second cancel answers the plan as it is
    expect(server.handle('POST', '/plans/plan-c81d44a0/validate', new URLSearchParams(), {}, 'operator').status).toBe(409);
    expect(server.handle('POST', '/plans/plan-c81d44a0/start', new URLSearchParams(), {}, 'operator').status).toBe(409);
    expect(call('plan-c81d44a0')).toMatchObject({ status: 200, body: { status: 'cancelled' } });
    expect(server.events.slice(before).filter((e) => e.kind === 'plan.cancelled')).toHaveLength(1);
  });

  it('cancels a removed VM\u2019s failed migration at validation like the API, unless its cutover stopped the source (SDD §5.4)', () => {
    const { server } = setup('operator');
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const mine = server.migrations.filter((m) => m.plan_id === plan.id);
    const dropped = mine[mine.length - 1]!;
    dropped.phase = 'failed';
    dropped.downtime_started_at = null;
    dropped.downtime_ended_at = null;
    plan.vm_ids = plan.vm_ids.filter((id) => id !== dropped.vm.source_id);
    // a stopped source refuses before anything changes
    dropped.downtime_started_at = '2026-10-08T11:50:00Z';
    dropped.downtime_ended_at = '2026-10-08T11:55:00Z';
    const refused = server.handle('POST', `/plans/${plan.id}/validate`, new URLSearchParams(), {}, 'operator');
    expect(refused.status).toBe(409);
    expect(dropped.phase).toBe('failed');
    // its source never stopped: cancelled, the VM released
    dropped.downtime_started_at = null;
    dropped.downtime_ended_at = null;
    expect(server.handle('POST', `/plans/${plan.id}/validate`, new URLSearchParams(), {}, 'operator').status).toBe(200);
    expect(dropped.phase).toBe('cancelled');
  });

  it('pages events back with `before`, like the API: with tail, the page below a seq; 0 is refused (SDD §12)', () => {
    const { server } = setup('viewer');
    const get = (q: string) => server.handle('GET', '/events', new URLSearchParams(q), undefined, 'viewer');
    const all = server.events.filter((e) => e.seq > 0);
    const newest = get('tail=true&limit=2').body as { seq: number }[];
    const older = get(`tail=true&limit=2&before=${newest[0]!.seq}`).body as { seq: number }[];
    expect(older.map((e) => e.seq)).toEqual(all.slice(-4, -2).map((e) => e.seq));
    expect(get('tail=true&before=0')).toMatchObject({ status: 422 });
  });

  it('refuses a secret name the control plane could not read, like the API: on POST and on the PATCH fields set (SDD §13.3)', () => {
    const { server } = setup('admin');
    const call = (method: string, path: string, body: unknown) => server.handle(method, path, new URLSearchParams(), body, 'admin');
    const vcenter = { id: 'vc-x', name: 'vCenter X', kind: 'vmware', role: 'source', endpoint: 'https://vc.example/sdk', distribution: 'vmware' };
    expect(call('POST', '/providers', { ...vcenter, credentials_secret: '../etc' })).toMatchObject({ status: 422 });
    expect(call('POST', '/providers', { ...vcenter, credentials_secret: 'vc-x.prod_1' })).toMatchObject({ status: 201, body: { credentials_secret: 'vc-x.prod_1' } });
    const host = { manage: false, address: '10.0.0.5', ssh_key_secret: 'has space' };
    expect(call('PATCH', '/providers/vc-x', { conversion_host: host })).toMatchObject({ status: 422 });
    expect(call('PATCH', '/providers/vc-x', { conversion_host: { ...host, ssh_key_secret: 'conv-key' } })).toMatchObject({ status: 200 });
    // another provider's managed secret is refused, the own allowed (SDD §13.3)
    expect(call('POST', '/providers', { ...vcenter, id: 'vc-w', credentials_secret: 'provider-vc-x' })).toMatchObject({ status: 422 });
    expect(call('POST', '/providers', { ...vcenter, id: 'vc-v', credentials_secret: 'provider-vc-v' })).toMatchObject({ status: 201 });
    expect(call('POST', '/providers', { ...vcenter, id: 'vc-u', credentials_secret: 'name\n' })).toMatchObject({ status: 422 });
    // an id whose managed names are another provider's is refused (SDD §13.3)
    expect(call('POST', '/providers', { ...vcenter, id: 'vc-x-ssh' })).toMatchObject({ status: 409 });
    expect(call('POST', '/providers', { ...vcenter, id: 'vc-x-sshd' })).toMatchObject({ status: 201 });
    // only the fields a PATCH sets are checked
    server.providers.find((p) => p.id === 'vc-x')!.credentials_secret = 'old name';
    expect(call('PATCH', '/providers/vc-x', { name: 'vCenter X2' })).toMatchObject({ status: 200 });
  });

  it('keeps a provider while a plan still needs it, like the API: until it completes and its VMs are finalized (SDD §12)', () => {
    const { server } = setup('admin');
    const plan = server.plans[0]!;
    const id = plan.source_provider_id;
    const users = server.plans.filter((p) => p.source_provider_id === id || p.destination_provider_id === id);
    for (const p of users) p.status = 'completed';
    const mine = server.migrations.filter((m) => users.some((p) => p.id === m.plan_id));
    for (const m of mine) m.phase = 'finalized';
    const remove = () => server.handle('DELETE', `/providers/${id}`, new URLSearchParams(), undefined, 'admin');
    const tail = 'a plan needs its providers until it completes and its VMs are finalized';
    // a failed plan can be started again (SDD §8)
    plan.status = 'failed';
    expect(remove()).toMatchObject({ status: 409, body: { error: { message: `provider is used by plan(s) that still need it: ${plan.id} ("${plan.name}", failed); ${tail}` } } });
    // a completed VM can be rolled back until it is finalized, and its rollback needs the provider (SDD §8.1)
    plan.status = 'completed';
    const [first, second] = mine.filter((m) => m.plan_id === plan.id);
    first!.phase = 'completed';
    expect(remove()).toMatchObject({ status: 409, body: { error: { message: `provider is used by plan(s) that still need it: ${plan.id} ("${plan.name}", 1 VM not finalized); ${tail}` } } });
    second!.phase = 'failed';
    expect(remove().body).toMatchObject({ error: { message: expect.stringContaining(`("${plan.name}", 2 VMs not finalized)`) } });
    // in creation order, whatever the list order; ten at most, then how many more
    const other = users.find((p) => p.id !== plan.id)!;
    other.status = 'draft';
    const [older, newer] = [plan, other].sort((a, b) => a.created_at.localeCompare(b.created_at));
    server.plans.reverse();
    const why = (p: Plan) => (p.id === plan.id ? '2 VMs not finalized' : 'draft');
    expect(remove().body).toMatchObject({
      error: { message: `provider is used by plan(s) that still need it: ${older!.id} ("${older!.name}", ${why(older!)}), ${newer!.id} ("${newer!.name}", ${why(newer!)}); ${tail}` },
    });
    const later = Date.parse(newer!.created_at);
    for (let i = 0; i < 9; i++) {
      server.plans.push({ ...other, id: `plan-extra00${i}`, name: `extra ${i}`, created_at: new Date(later + (i + 1) * 1000).toISOString() });
    }
    const listed = (remove().body as { error: { message: string } }).error.message;
    expect(listed).toContain('plan-extra007 ("extra 7", draft) and 1 more; ');
    expect(listed).not.toContain('plan-extra008');
    server.plans.splice(server.plans.findIndex((p) => p.id === 'plan-extra000'), 9);
    other.status = 'completed';
    // a rolled-back VM that kept no copied data lets it go: a retry refuses when the provider is gone (SDD §5.1)
    first!.phase = 'rolled_back';
    first!.sync_passes = [];
    second!.phase = 'cancelled';
    expect(remove().status).toBe(204);
  });

  it('keeps a provider while a rolled-back VM keeps its copied data, like the API: until a cancel cleans it up (SDD §12, §5.1)', async () => {
    const { server, client } = setup('admin');
    const rolled = byPhase(server, 'rolled_back');
    const plan = server.plans.find((p) => p.id === rolled.plan_id)!;
    const id = plan.destination_provider_id;
    const users = server.plans.filter((p) => p.source_provider_id === id || p.destination_provider_id === id);
    for (const p of users) p.status = 'completed';
    const passes = server.migrations.find((m) => m.vm.name === 'app-billing-01')!.sync_passes.map((p) => ({ ...p }));
    for (const m of server.migrations.filter((x) => users.some((p) => p.id === x.plan_id))) m.phase = 'finalized';
    const remove = () => server.handle('DELETE', `/providers/${id}`, new URLSearchParams(), undefined, 'admin');
    const tail = 'a plan needs its providers until it completes and its VMs are finalized';
    const provider = server.providers.find((p) => p.id === id)!;
    // a rolled-back VM that recorded no passes kept nothing: it lets the provider go
    rolled.phase = 'rolled_back';
    rolled.sync_passes = [];
    expect(remove().status).toBe(204);
    server.providers.push(provider);
    // a warm rollback kept the destination volumes and warm state for a retry, and a cancel cleans them up with the provider
    rolled.strategy = 'warm';
    rolled.sync_passes = passes;
    expect(remove()).toMatchObject({
      status: 409,
      body: { error: { message: `provider is used by plan(s) that still need it: ${plan.id} ("${plan.name}", 1 rolled-back VM keeping its copied data); ${tail}` } },
    });
    const second = server.migrations.find((m) => m.plan_id === plan.id && m.id !== rolled.id)!;
    second.phase = 'rolled_back';
    second.strategy = 'warm';
    second.sync_passes = passes.map((p) => ({ ...p }));
    expect(remove().body).toMatchObject({ error: { message: expect.stringContaining(`("${plan.name}", 2 rolled-back VMs keeping their copied data)`) } });
    second.phase = 'completed';
    expect(remove().body).toMatchObject({
      error: { message: expect.stringContaining(`("${plan.name}", 1 VM not finalized, 1 rolled-back VM keeping its copied data)`) },
    });
    second.phase = 'finalized';
    // a cold rollback deleted what the cold import copied, though its cutover lists a pass (SDD §5.1, §12)
    rolled.strategy = 'cold';
    expect(remove().status).toBe(204);
    server.providers.push(provider);
    rolled.strategy = 'warm';
    // the cancel cleans up what the rollback kept (the mock's cleanup ends at once): the provider goes
    await client.post<Migration>(`/migrations/${rolled.id}/cancel`, { reason: 'not migrated after all' });
    expect(remove().status).toBe(204);
  });

  it('refuses a rollback whose provider no longer exists, like the API: nothing changes (SDD §5.1)', async () => {
    const { server, client } = setup('operator');
    const done = byPhase(server, 'completed');
    const plan = server.plans.find((p) => p.id === done.plan_id)!;
    server.providers.splice(server.providers.findIndex((p) => p.id === plan.destination_provider_id), 1);
    await expect(client.post(`/migrations/${done.id}/rollback`, { reason: 'app broken' })).rejects.toMatchObject({
      status: 409,
      message: `${done.vm.name} cannot be rolled back: the plan's destination provider '${plan.destination_provider_id}' no longer exists`,
    });
    expect(done.phase).toBe('completed');
    // a migration that cannot roll back says so first, like the API
    const ready = server.migrations.find((m) => m.plan_id === plan.id && m.id !== done.id)!;
    ready.phase = 'ready';
    await expect(client.post(`/migrations/${ready.id}/rollback`, { reason: 'x' })).rejects.toMatchObject({
      status: 409,
      message: `Cannot move ${ready.vm.name} from ready to rolling_back.`,
    });
  });

  it('accepts approvals and cutover requests in the phases the API does (SDD §5.4)', () => {
    const { server } = setup('approver');
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a12')!;
    const post = (action: string) => server.handle('POST', `/migrations/${m.id}/${action}`, new URLSearchParams(), {}, 'approver');
    m.phase = 'pending';
    expect(post('approve').status).toBe(200);
    m.phase = 'verifying';
    expect(post('approve')).toEqual({ status: 409, body: { error: { code: 'conflict', message: 'a migration in verifying cannot be approved' } } });
    expect(post('cutover')).toEqual({ status: 409, body: { error: { code: 'conflict', message: 'cutover cannot be requested in verifying' } } });
    // a warm cutover requested during pre-copy waits for convergence
    m.phase = 'precopy';
    m.cutover_requested = false;
    expect(post('cutover').status).toBe(200);
    expect(m).toMatchObject({ phase: 'precopy', cutover_requested: true });
  });

  it('plans waves on a validated or paused plan and returns it to draft, like the API (SDD §12)', () => {
    const { server } = setup('operator');
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    for (const status of ['validated', 'paused'] as const) {
      plan.status = status;
      const result = server.handle('POST', `/plans/${plan.id}/waves/auto`, new URLSearchParams(), { max_wave_size: 5 }, 'operator');
      expect(result.status).toBe(200);
      expect(plan.status).toBe('draft');
    }
  });
});

describe('mock advisor recommendations, like the API (SDD §12, §14.4)', () => {
  const list = (client: ApiClient, query: Record<string, string | number | undefined> = {}) => client.get<RecommendationPage>('/advisor/recommendations', { query });

  it('lists stored and legacy recommendations newest first, a legacy entry for each strategy note', async () => {
    const { server, client } = setup('viewer');
    const page = await list(client);
    expect([page.offset, page.limit, page.total]).toEqual([0, 50, page.items.length]);
    const order = page.items.map((r) => `${r.created_at} ${r.id}`);
    expect(order).toEqual([...order].sort().reverse());
    const strategyNotes = server.migrations.flatMap((m) => m.advisor_notes.filter((n) => n.kind === 'strategy'));
    const legacy = page.items.filter((r) => r.legacy);
    expect(strategyNotes.length).toBeGreaterThan(0);
    expect(legacy.map((r) => r.legacy_text)).toEqual(expect.arrayContaining(strategyNotes.map((n) => n.summary)));
    expect(legacy).toHaveLength(strategyNotes.length);
    expect(legacy.every((r) => r.id.startsWith('rec-legacy-') && r.decision.actor.id === 'legacy')).toBe(true);
  });

  it('seeds every decision status, and the spec example: 57% under a 60% threshold keeps cold', async () => {
    const { client } = setup('viewer');
    const stored = (await list(client)).items.filter((r) => !r.legacy);
    expect(new Set(stored.map((r) => r.decision.status))).toEqual(new Set(['auto_applied', 'below_threshold', 'fallback_rules', 'pending_approval', 'approved', 'overridden']));
    const kept = stored.find((r) => r.decision.status === 'below_threshold')!;
    expect([kept.recommendation.option, kept.recommendation.confidence, kept.decision.threshold_used, kept.decision.final_option]).toEqual(['warm', 0.57, 0.6, 'cold']);
    const fallback = stored.find((r) => r.decision.status === 'fallback_rules')!;
    expect([fallback.model.name, fallback.recommendation.option, fallback.decision.actor]).toEqual(['rules', null, { kind: 'system', id: 'fallback' }]);
  });

  it('runs every fixture migration on the final option of its newest decision', () => {
    const server = new MockServer({ now: () => NOW });
    const newest = new Map<string, AdvisorRecommendation>();
    for (const r of server.recommendations) {
      const seen = newest.get(r.migration_id!);
      if (!seen || seen.created_at < r.created_at) newest.set(r.migration_id!, r);
    }
    expect(newest.size).toBeGreaterThan(0);
    for (const [id, r] of newest) {
      const m = server.migrations.find((x) => x.id === id)!;
      // a decision that waits has no final option yet: the migration keeps the deterministic choice and is held
      if (r.decision.final_option === null) expect([r.decision.status, m.advisor_hold], id).toEqual(['pending_approval', r.id]);
      else expect(m.strategy, id).toBe(r.decision.final_option);
    }
  });

  it('filters by migration, plan and decision type, and pages with limit and offset', async () => {
    const { client } = setup('viewer');
    const all = await list(client, { limit: 200 });
    const target = all.items.find((r) => !r.legacy)!;
    const mine = await list(client, { migration_id: target.migration_id! });
    expect(mine.items.length).toBeGreaterThan(0);
    expect(mine.items.every((r) => r.migration_id === target.migration_id)).toBe(true);
    expect((await list(client, { plan_id: target.plan_id! })).total).toBe(all.items.filter((r) => r.plan_id === target.plan_id).length);
    expect(await list(client, { decision_type: 'workload_tier' })).toMatchObject({ items: [], total: 0 });
    const second = await list(client, { limit: 2, offset: 2 });
    expect(second.items.map((r) => r.id)).toEqual(all.items.slice(2, 4).map((r) => r.id));
    expect(second).toMatchObject({ total: all.total, offset: 2, limit: 2 });
  });

  it('refuses a limit outside 1-200, a negative offset and an unknown decision type with 422', async () => {
    const { client } = setup('viewer');
    for (const query of [{ limit: 0 }, { limit: 201 }, { limit: 'many' }, { offset: -1 }, { offset: 1.5 }, { decision_type: 'strategy' }]) {
      await expect(list(client, query), JSON.stringify(query)).rejects.toMatchObject({ status: 422, code: 'validation_error' });
    }
  });

  it('gets one recommendation by id, a legacy one too, and answers 404 for an unknown id', async () => {
    const { client } = setup('viewer');
    const { items } = await list(client, { limit: 200 });
    for (const r of [items.find((x) => !x.legacy)!, items.find((x) => x.legacy)!]) {
      expect(await client.get<AdvisorRecommendation>(`/advisor/recommendations/${r.id}`)).toEqual(r);
    }
    await expect(client.get('/advisor/recommendations/rec-000000000000')).rejects.toMatchObject({ status: 404 });
    await expect(client.get('/advisor/recommendations/rec-legacy-mig-b2c4d6e870-9')).rejects.toMatchObject({ status: 404 });
  });

  it('asks for a token, like the API', async () => {
    const { client } = setup('nobody');
    await expect(list(client)).rejects.toMatchObject({ status: 401 });
  });
});

describe('mock migration summaries, like the API (SDD §4, §12, perf audit PF-02)', () => {
  it('lists each migration as its summary, in the same order, filtered and paged alike; another view is 422', async () => {
    const { client } = setup('viewer');
    const full = await client.get<Migration[]>('/migrations');
    expect(await client.get<MigrationSummary[]>('/migrations', { query: { view: 'summary' } })).toEqual(full.map(summaryOf));
    const planId = full[0]!.plan_id;
    const page = { plan_id: planId, limit: 2, offset: 1 };
    expect(await client.get('/migrations', { query: { ...page, view: 'summary' } })).toEqual((await client.get<Migration[]>('/migrations', { query: page })).map(summaryOf));
    expect(await client.get('/migrations', { query: { view: 'full' } })).toEqual(full);
    await expect(client.get('/migrations', { query: { view: 'everything' } })).rejects.toMatchObject({ status: 422 });
  });
});

describe('mock advisor notes, like the API (SDD §12, perf audit PF-02)', () => {
  const notes = (client: ApiClient, query: Record<string, string | number | undefined> = {}) => client.get<AdvisorNoteList>('/advisor/notes', { query });
  const note = (kind: AdvisorNote['kind'], at: string, summary: string): AdvisorNote => ({ kind, source: 'rules', summary, confidence: null, data: {}, created_at: at });

  it("lists every migration's notes but strategy notes, newest first; the same time keeps creation and note order", async () => {
    const { server, client } = setup('viewer');
    for (const m of server.migrations) m.advisor_notes = [];
    const [first, second] = server.migrations as [Migration, Migration];
    first.advisor_notes = [note('classification', '2026-10-08T09:00:00Z', 'tier'), note('strategy', '2026-10-08T11:00:00Z', 'legacy'), note('screen', '2026-10-08T08:00:00Z', 'screened')];
    second.advisor_notes = [note('verification', '2026-10-08T10:00:00Z', 'boot'), note('similar_incidents', '2026-10-08T09:00:00Z', 'similar')];
    const { items } = await notes(client);
    expect(items.map((n) => [n.migration_id, n.kind])).toEqual([
      [second.id, 'verification'],
      [first.id, 'classification'],
      [second.id, 'similar_incidents'],
      [first.id, 'screen'],
    ]);
    expect(items[0]).toEqual({ ...second.advisor_notes[0], migration_id: second.id, plan_id: second.plan_id, vm_name: second.vm.name });
    expect((await notes(client, { limit: 2 })).items.map((n) => n.kind)).toEqual(['verification', 'classification']);
    // 20 unless asked otherwise, at most 200
    second.advisor_notes.push(...Array.from({ length: 25 }, (_, i) => note('classification', new Date(Date.parse('2026-10-07T00:00:00Z') - i * 60_000).toISOString(), `tier ${i}`)));
    expect((await notes(client)).items).toHaveLength(20);
    expect((await notes(client, { limit: 200 })).items).toHaveLength(29);
    for (const limit of [0, 201]) await expect(notes(client, { limit })).rejects.toMatchObject({ status: 422 });
  });

  it('needs a token, and any role', async () => {
    // no token at all is auth turned off, as in the API: an anonymous admin
    await expect(notes(setup('nonsense').client)).rejects.toMatchObject({ status: 401 });
    expect((await notes(setup('viewer').client)).items.length).toBeGreaterThan(0);
  });
});

describe('mock audit trail, like the API (SDD §14.5)', () => {
  it('lists only audit events with audit=true, newest last', async () => {
    const { client } = setup('viewer');
    const audit = await client.get<Event[]>('/events', { query: { audit: true, tail: true, limit: 1000 } });
    expect(audit.length).toBeGreaterThan(0);
    expect(audit.every((e) => (AUDIT_EVENT_KINDS as readonly string[]).includes(e.kind))).toBe(true);
    expect(audit.map((e) => e.seq)).toEqual([...audit.map((e) => e.seq)].sort((a, b) => a - b));
  });

  it('seeds one recommendation.created and one decision event per status a decision took', () => {
    const server = new MockServer({ now: () => NOW });
    for (const rec of server.recommendations) {
      const mine = server.events.filter((e) => e.data.target_id === rec.id).map((e) => e.kind);
      const current = rec.decision.status === 'fallback_rules' ? 'decision.fallback' : `decision.${rec.decision.status}`;
      expect(mine[0], rec.id).toBe('recommendation.created');
      expect(mine.at(-1), rec.id).toBe(current);
      // a person's decision follows the validation's: two decision events, else one
      expect(mine.length, rec.id).toBe(['approved', 'overridden'].includes(rec.decision.status) ? 3 : 2);
      expect(server.snapshots.has(rec.input_snapshot_sha256!), rec.id).toBe(true);
    }
  });

  it('serves an input snapshot anyone can re-hash, 404 unknown, 422 malformed', async () => {
    const { server, client } = setup('viewer');
    const sha = server.recommendations[0]!.input_snapshot_sha256!;
    const snapshot = await client.get<{ sha256: string; payload: unknown }>(`/advisor/snapshots/${sha}`);
    expect(sha256Hex(canonicalJson(snapshot.payload))).toBe(sha);
    await expect(client.get(`/advisor/snapshots/${'0'.repeat(64)}`)).rejects.toMatchObject({ status: 404 });
    await expect(client.get('/advisor/snapshots/not-a-hash')).rejects.toMatchObject({ status: 422 });
  });
});

describe('mock advisor policies, approval and override, like the API (SDD §14.6)', () => {
  const ADMIN = 'admin';
  function as(server: MockServer, token: string) {
    return new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  }
  const recOf = (server: MockServer, vm: string) => server.recommendations.filter((r) => r.subject.name === vm).at(-1)!;

  it('seeds the Finance policy and decisions that wait, were approved and were overridden', () => {
    const server = new MockServer({ now: () => NOW });
    expect(server.policies.map((p) => [p.name, p.mode, p.scope.provider_ids, p.require_override_reason])).toEqual([
      ['Finance — approval required', 'always_require_approval', ['rhosp17-dc1'], true],
    ]);
    const waiting = recOf(server, 'analytics-node-2');
    expect([waiting.decision.status, waiting.decision.final_option, waiting.decision.policy_id]).toEqual(['pending_approval', null, 'pol-finance']);
    expect(server.migrations.find((m) => m.vm.name === 'analytics-node-2')!.advisor_hold).toBe(waiting.id);
    const approved = recOf(server, 'vm-hr-portal');
    expect([approved.decision.status, approved.decision.final_option, approved.decision.actor]).toEqual(['approved', 'vmware_warm', { kind: 'user', id: 'sari' }]);
    expect(server.events.filter((e) => e.data.target_id === approved.id).map((e) => e.kind)).toEqual(['recommendation.created', 'decision.below_threshold', 'decision.approved']);
    const overridden = recOf(server, 'ad-dc-01');
    expect([overridden.decision.status, overridden.decision.final_option, (overridden.decision.reason_text?.length ?? 0) >= 10]).toEqual(['overridden', 'cold', true]);
    expect(server.plans.find((p) => p.id === 'plan-4f2a9c1e')!.strategy_overrides).toMatchObject({ 'os-0c41': 'cold' });
    const policyCreated = server.events.filter((e) => e.kind === 'policy.created');
    expect(policyCreated.map((e) => [e.actor, e.data.target_id])).toEqual([['user:rina', 'pol-finance']]);
  });

  it('decides under the Finance policy: the decision waits and holds the migration, whatever the confidence', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'cold', confidence: 0.99 }) });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate');
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    const rec = recOf(server, 'analytics-db');
    expect([rec.decision.status, rec.decision.final_option, rec.decision.reason_code]).toEqual(['pending_approval', null, 'POLICY_REQUIRES_APPROVAL']);
    expect([db.advisor_hold, db.strategy]).toEqual([rec.id, 'warm']);
    expect(server.events.filter((e) => e.data.target_id === rec.id).map((e) => e.kind)).toEqual(['recommendation.created', 'decision.pending_approval']);
  });

  it('starts a held migration only once a human decides: approve, then the run goes on', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate');
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    await as(server, 'operator').post(`/plans/${plan.id}/start`);
    for (let i = 0; i < 5; i += 1) server.tick();
    expect(db.phase).toBe('ready');
    const rec = await as(server, ADMIN).post<AdvisorRecommendation>(`/advisor/recommendations/${recOf(server, 'analytics-db').id}/approve`, {});
    expect([rec.decision.status, rec.decision.final_option, rec.decision.actor.id]).toEqual(['approved', 'warm', 'rina']);
    expect(db.advisor_hold).toBeNull();
    server.tick();
    expect(db.phase).toBe('precopy');
  });

  it('runs an overridden option, keeps it in the plan and needs the reason the policy asks for', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate');
    const id = recOf(server, 'analytics-db').id;
    const url = `/advisor/recommendations/${id}/override`;
    for (const reason of [undefined, '', 'too short', ' '.repeat(12)]) {
      await expect(as(server, ADMIN).post(url, { option: 'cold', reason }), String(reason)).rejects.toMatchObject({ status: 422 });
    }
    await expect(as(server, ADMIN).post(url, { option: 'storage_handover', reason: 'No shared Ceph between the clouds' })).rejects.toMatchObject({ status: 422 });
    const rec = await as(server, ADMIN).post<AdvisorRecommendation>(url, { option: 'cold', reason: 'The change window fits a cold cutover' });
    expect([rec.decision.status, rec.decision.final_option, rec.decision.reason_text]).toEqual(['overridden', 'cold', 'The change window fits a cold cutover']);
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    expect([db.strategy, db.advisor_hold]).toEqual(['cold', null]);
    expect(server.plans.find((p) => p.id === 'plan-c81d44a0')!.strategy_overrides).toMatchObject({ [db.vm.source_id]: 'cold' });
    // the trail itself says why (SDD §14.5)
    expect(server.events.filter((e) => e.data.target_id === id).at(-1)).toMatchObject({
      kind: 'decision.overridden',
      actor: 'user:rina',
      data: { before: { status: 'pending_approval', reason_text: null }, after: { status: 'overridden', reason_text: 'The change window fits a cold cutover' } },
    });
  });

  it('refuses what the API refuses: 403 below admin, 409 for a legacy, an older or a started one, and PUT …/strategy while held', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    const held = server.migrations.find((m) => m.vm.name === 'analytics-node-2')!;
    const id = held.advisor_hold!;
    for (const token of ['viewer', 'operator', 'approver']) {
      await expect(as(server, token).post(`/advisor/recommendations/${id}/approve`, {})).rejects.toMatchObject({ status: 403 });
    }
    await expect(as(server, 'operator').put(`/migrations/${held.id}/strategy`, { strategy: 'warm' })).rejects.toMatchObject({ status: 409, message: expect.stringContaining('waits for approval') });
    await expect(as(server, ADMIN).post('/advisor/recommendations/rec-legacy-mig-b2c4d6e870-0/approve', {})).rejects.toMatchObject({ status: 409 });
    await expect(as(server, ADMIN).post(`/advisor/recommendations/${recOf(server, 'web-01').id}/approve`, {})).rejects.toMatchObject({ status: 409 });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate'); // a newer recommendation for analytics-db
    const older = server.recommendations.filter((r) => r.subject.name === 'analytics-db');
    expect(older.length).toBeGreaterThan(0);
  });

  it('lists the policies with the built-in default and audits every change by advisor.policy.manage', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const list = await as(server, 'viewer').get<{ items: unknown[]; default: { id: string; mode: string; threshold: number } }>('/advisor/policies');
    expect([list.items.length, list.default.id, list.default.mode, list.default.threshold]).toEqual([1, 'global-default', 'auto_apply_above_threshold', 0.6]);
    const spec = { name: 'Analytics — stricter', scope: { provider_ids: [], plan_ids: ['plan-c81d44a0'] }, decision_types: ['migration_strategy'], mode: 'auto_apply_above_threshold', threshold: 0.8, min_evidence_for_auto_apply: 0, require_override_reason: false };
    await expect(as(server, 'approver').post('/advisor/policies', spec)).rejects.toMatchObject({ status: 403 });
    for (const bad of [{ scope: { provider_ids: ['rhosp17-dc1'], plan_ids: ['plan-c81d44a0'] } }, { scope: { provider_ids: ['nope'], plan_ids: [] } }, { threshold: 1.5 }, { name: '' }]) {
      await expect(as(server, ADMIN).post('/advisor/policies', { ...spec, ...bad }), JSON.stringify(bad)).rejects.toMatchObject({ status: 422 });
    }
    const created = await as(server, ADMIN).post<{ id: string }>('/advisor/policies', spec);
    await as(server, ADMIN).put(`/advisor/policies/${created.id}`, { ...spec, threshold: 0.85 });
    await as(server, ADMIN).delete(`/advisor/policies/${created.id}`);
    await expect(as(server, ADMIN).delete(`/advisor/policies/${created.id}`)).rejects.toMatchObject({ status: 404 });
    expect(server.events.filter((e) => e.data.target_id === created.id).map((e) => [e.kind, e.actor])).toEqual([
      ['policy.created', 'user:rina'],
      ['policy.updated', 'user:rina'],
      ['policy.deleted', 'user:rina'],
    ]);
  });
});

describe('mock validation decides like the API (SDD §14.4)', () => {
  function validating(advice: { option: string | null; confidence: number | null }) {
    const advise = vi.fn(() => advice);
    const server = new MockServer({ now: () => NOW, seed: 1, advise });
    server.policies.length = 0; // these run under the built-in global default (SDD §14.6)
    const client = new ApiClient({ getToken: () => 'operator', fetchImpl: createMockFetch(server) });
    // the analytics plan: analytics-db ties cold and warm (nothing meets the SLO), the handover nodes do not
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    return { server, client, plan, db, advise, before: server.recommendations.length };
  }

  it('records the decision on an automatic choice between strategies, and the migration runs its final option', async () => {
    const { server, client, plan, db, advise, before } = validating({ option: 'cold', confidence: 0.8 });
    expect(db.strategy).toBe('warm');
    await client.post(`/plans/${plan.id}/validate`);
    expect(advise).toHaveBeenCalledTimes(1);
    const [rec, ...more] = server.recommendations.slice(before);
    expect(more).toEqual([]);
    expect(rec).toMatchObject({
      decision_type: 'migration_strategy',
      subject: { kind: 'vm', id: db.vm.source_id, name: 'analytics-db' },
      plan_id: plan.id,
      migration_id: db.id,
      provider_id: plan.source_provider_id,
      model: { name: 'jev' },
      recommendation: { option: 'cold', confidence: 0.8 },
      decision: { final_option: 'cold', status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', policy_id: 'global-default', threshold_used: 0.6 },
    });
    expect(db.strategy).toBe('cold');
    // SDD §14.5: recommendation.created, then exactly one decision event; advisor.strategy is gone
    const audit = server.events.filter((e) => e.migration_id === db.id && (AUDIT_EVENT_KINDS as readonly string[]).includes(e.kind));
    expect(audit.map((e) => [e.kind, e.actor])).toEqual([
      ['recommendation.created', 'system:advisor'],
      ['decision.auto_applied', 'policy:global-default'],
    ]);
    expect(audit[0]!.data).toMatchObject({ target_id: rec!.id, model_version: 'jev-1.13.0', option: 'cold', confidence: 0.8, snapshot_sha256: rec!.input_snapshot_sha256 });
    expect(audit[1]!.data).toEqual({
      target_id: rec!.id,
      model_version: 'jev-1.13.0',
      before: null,
      after: { status: 'auto_applied', final_option: 'cold', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', reason_text: null, policy_id: 'global-default', threshold_used: 0.6 },
    });
    expect(server.events.filter((e) => e.migration_id === db.id && e.kind === 'advisor.strategy')).toEqual([]);
    // the input snapshot is named by the SHA-256 of its canonical JSON
    const snapshot = server.snapshots.get(rec!.input_snapshot_sha256!)!;
    expect(sha256Hex(canonicalJson(snapshot.payload))).toBe(snapshot.sha256);
    expect(snapshot.payload).toMatchObject({ tool: 'jev_decide', scope: ['cold', 'warm'], deterministic: 'warm' });
  });

  it('keeps the deterministic choice when the confidence is below the threshold', async () => {
    const { server, client, plan, db, before } = validating({ option: 'cold', confidence: 0.57 });
    await client.post(`/plans/${plan.id}/validate`);
    expect(server.recommendations.slice(before).map((r) => [r.recommendation.option, r.decision.status, r.decision.final_option])).toEqual([['cold', 'below_threshold', 'warm']]);
    expect(db.strategy).toBe('warm');
  });

  it('falls back to the rules when the advisor gives no answer', async () => {
    const { server, client, plan, db, before } = validating({ option: null, confidence: null });
    await client.post(`/plans/${plan.id}/validate`);
    const [rec] = server.recommendations.slice(before);
    expect([rec!.model.name, rec!.decision.status, rec!.decision.final_option, rec!.decision.actor.id]).toEqual(['rules', 'fallback_rules', 'warm', 'fallback']);
    expect(db.strategy).toBe('warm');
  });

  it.each([
    ['an option outside the scope', 'storage_handover'],
    ['an escape hatch', 'ask_user'],
  ])('falls back to the rules on %s, which is no usable answer', async (_, option) => {
    const { server, client, plan, db, before } = validating({ option, confidence: 0.9 });
    await client.post(`/plans/${plan.id}/validate`);
    const [rec] = server.recommendations.slice(before);
    expect([rec!.model.name, rec!.decision.status, rec!.decision.final_option]).toEqual(['jev', 'fallback_rules', 'warm']);
    expect(rec!.decision.reason_text).toContain(option);
    expect(db.strategy).toBe('warm');
  });

  it('asks nothing when the plan or an override makes the choice', async () => {
    const { server, client, plan, advise, before } = validating({ option: 'cold', confidence: 0.9 });
    plan.default_strategy = 'warm';
    await client.post(`/plans/${plan.id}/validate`);
    expect(advise).not.toHaveBeenCalled();
    expect(server.recommendations).toHaveLength(before);
  });

  it('simulates the advisor the same way every time, without drawing from the seeded random stream', async () => {
    const runs = await Promise.all(
      [1, 2].map(async () => {
        const server = new MockServer({ now: () => NOW, seed: 1 });
        const client = new ApiClient({ getToken: () => 'operator', fetchImpl: createMockFetch(server) });
        const before = server.recommendations.length;
        await client.post('/plans/plan-c81d44a0/validate');
        const [rec] = server.recommendations.slice(before);
        return { rec: rec!.recommendation, next: (server as unknown as { rand: () => number }).rand() };
      }),
    );
    expect(runs[0]).toEqual(runs[1]);
    expect(runs[0]!.rec.confidence).toBeGreaterThanOrEqual(0.5);
    expect(runs[0]!.rec.confidence).toBeLessThan(0.95);
    expect(runs[0]!.next).toBe((new MockServer({ now: () => NOW, seed: 1 }) as unknown as { rand: () => number }).rand());
  });
});

describe('mock maker-checker, like the API (SDD §5.4, §14.6, ADV-17)', () => {
  const ADMIN_TOKEN = 'admin';
  function as(server: MockServer, token: string) {
    return new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  }
  const OWN_CUTOVER = 'You requested this cutover. Another approver must approve it.';
  const OWN_DECISION = 'You requested this decision. Another approver must decide it.';

  it('records a cutover request without an approval, refuses the requester\'s approval and opens the gate on another approver\'s', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'storage_handover', confidence: 0.99 }) });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate');
    await as(server, 'operator').post('/plans/plan-c81d44a0/start');
    const node = server.migrations.find((m) => m.vm.name === 'analytics-node-1')!;
    const requested = await as(server, 'approver').post<Migration>(`/migrations/${node.id}/cutover`, { comment: 'Window agreed with the data team' });
    expect([requested.cutover_requested, requested.cutover_requested_by, requested.approvals]).toEqual([true, 'sari', []]);
    expect(requested.cutover_requested_at).not.toBeNull();
    expect(server.events.filter((e) => e.migration_id === node.id && e.kind === 'migration.action').at(-1)!.data.comment).toBe('Window agreed with the data team');
    await expect(as(server, 'approver').post(`/migrations/${node.id}/approve`, { comment: 'mine' })).rejects.toMatchObject({ status: 409, message: OWN_CUTOVER });
    for (let i = 0; i < 5; i += 1) server.tick();
    expect(node.phase).toBe('ready');
    await as(server, ADMIN_TOKEN).post(`/migrations/${node.id}/approve`, { comment: 'Checked the handover findings' });
    for (let i = 0; i < 3 && node.phase === 'ready'; i += 1) server.tick();
    expect(node.phase).toBe('cutover');
  });

  it('refuses a decision while the plan sets the VM\'s strategy, and a second approve, like the API (SDD §14.6)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const held = server.migrations.find((m) => m.advisor_hold)!;
    const recId = held.advisor_hold!;
    const plan = server.plans.find((p) => p.id === held.plan_id)!;
    plan.strategy_overrides = { ...plan.strategy_overrides, [held.vm.source_id]: 'cold' };
    const pinned = `The plan sets the strategy of ${held.vm.name} (cold): change it in the plan.`;
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${recId}/approve`, {})).rejects.toMatchObject({ status: 409, message: pinned });
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${recId}/override`, { option: 'cold', reason: 'the owner chose cold' })).rejects.toMatchObject({
      status: 409,
      message: pinned,
    });
    const { [held.vm.source_id]: _, ...rest } = plan.strategy_overrides;
    plan.strategy_overrides = rest;
    await as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${recId}/approve`, {});
    const approvals = server.events.filter((e) => e.kind === 'decision.approved').length;
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${recId}/approve`, {})).rejects.toMatchObject({
      status: 409,
      message: 'The decision is already approved.',
    });
    expect(server.events.filter((e) => e.kind === 'decision.approved')).toHaveLength(approvals);
    // another option is still a person's call while nothing started
    const overridden = await as(server, ADMIN_TOKEN).post<AdvisorRecommendation>(`/advisor/recommendations/${recId}/override`, {
      option: 'cold',
      reason: 'the owner chose cold',
    });
    expect(overridden.decision.status).toBe('overridden');
  });

  it('clears the advisor hold of a migration it cancels, like the API (SDD §14.6)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const held = server.migrations.find((m) => m.advisor_hold)!;
    await as(server, 'operator').post(`/migrations/${held.id}/cancel`, { reason: 'not this one' });
    expect([held.phase, held.advisor_hold]).toEqual(['cancelled', null]);
    // and a validation that cancels the migration of a VM that left the plan
    const other = new MockServer({ now: () => NOW, seed: 1 });
    const kept = other.migrations.find((m) => m.advisor_hold)!;
    const plan = other.plans.find((p) => p.id === kept.plan_id)!;
    plan.vm_ids = plan.vm_ids.filter((id) => id !== kept.vm.source_id);
    await as(other, 'operator').post(`/plans/${plan.id}/validate`);
    expect([kept.phase, kept.advisor_hold]).toEqual(['cancelled', null]);
  });

  it('refuses the approval of the approver who forced the cutover, like the API (SDD §5.4)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const warm = byPhase(server, 'syncing');
    await as(server, ADMIN_TOKEN).post(`/migrations/${warm.id}/cutover`, { comment: 'Window agreed' });
    await as(server, 'approver').post(`/migrations/${warm.id}/force-cutover`, { reason: 'the window closes in ten minutes' });
    await expect(as(server, 'approver').post(`/migrations/${warm.id}/approve`, { comment: 'my own force' })).rejects.toMatchObject({
      status: 409,
      message: 'You forced this cutover. Another approver must approve it.',
    });
    expect(warm.approvals.map((a) => a.actor)).not.toContain('sari');
  });

  it("does not count an approval the requester gave before requesting: the gate waits for another approver's", async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'storage_handover', confidence: 0.99 }) });
    await as(server, 'operator').post('/plans/plan-c81d44a0/validate');
    await as(server, 'operator').post('/plans/plan-c81d44a0/start');
    const node = server.migrations.find((m) => m.vm.name === 'analytics-node-1')!;
    await as(server, 'approver').post(`/migrations/${node.id}/approve`, { comment: 'Findings checked with the data team' });
    await as(server, 'approver').post(`/migrations/${node.id}/cutover`, {});
    for (let i = 0; i < 5; i += 1) server.tick();
    expect([node.phase, node.approvals.map((a) => a.actor), node.cutover_requested_by]).toEqual(['ready', ['sari'], 'sari']);
  });

  it('treats a request as an approval, as before, when the plan needs no distinct approver', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const node = server.migrations.find((m) => m.vm.name === 'analytics-node-1')!;
    server.plans.find((p) => p.id === node.plan_id)!.require_distinct_approver = false;
    const requested = await as(server, 'approver').post<Migration>(`/migrations/${node.id}/cutover`, {});
    expect(requested.approvals.map((a) => a.actor)).toEqual(['sari']);
  });

  it('keeps the rules of the policy that took a decision when the policy is relaxed later, like the API (SDD §14.6)', async () => {
    const relax = (server: MockServer) => {
      for (const p of server.policies) Object.assign(p, { require_distinct_approver: false, require_override_reason: false });
    };
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    await as(server, ADMIN_TOKEN).post('/plans/plan-c81d44a0/validate');
    const rec = server.recommendations.filter((r) => r.subject.name === 'analytics-db').at(-1)!;
    expect([rec.requested_by, rec.decision.require_distinct_approver, rec.decision.require_override_reason]).toEqual(['rina', true, true]);
    relax(server);
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${rec.id}/approve`, {})).rejects.toMatchObject({ status: 409, message: OWN_DECISION });
    // validated by another user: the override still needs its reason
    const other = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    await as(other, 'operator').post('/plans/plan-c81d44a0/validate');
    const held = other.recommendations.filter((r) => r.subject.name === 'analytics-db').at(-1)!;
    relax(other);
    await expect(as(other, ADMIN_TOKEN).post(`/advisor/recommendations/${held.id}/override`, { option: 'cold' })).rejects.toMatchObject({ status: 422 });
    const overridden = await as(other, ADMIN_TOKEN).post<AdvisorRecommendation>(`/advisor/recommendations/${held.id}/override`, {
      option: 'cold',
      reason: 'The change window fits a cold cutover',
    });
    // the rules go on with a person's decision
    expect([overridden.decision.require_distinct_approver, overridden.decision.require_override_reason]).toEqual([true, true]);
    // the fallback took no policy's decision: it records no rules, so the policy in force applies
    const unavailable = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: null, confidence: null }) });
    await as(unavailable, 'operator').post('/plans/plan-c81d44a0/validate');
    const fallback = unavailable.recommendations.filter((r) => r.subject.name === 'analytics-db').at(-1)!;
    expect([fallback.decision.status, fallback.decision.require_distinct_approver, fallback.decision.require_override_reason]).toEqual(['fallback_rules', null, null]);
  });

  it('names the validating user as the requester of a decision, who then neither approves nor overrides it', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => ({ option: 'warm', confidence: 0.99 }) });
    await as(server, ADMIN_TOKEN).post('/plans/plan-c81d44a0/validate');
    const rec = server.recommendations.filter((r) => r.subject.name === 'analytics-db').at(-1)!;
    expect([rec.requested_by, rec.decision.status]).toEqual(['rina', 'pending_approval']);
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${rec.id}/approve`, {})).rejects.toMatchObject({ status: 409, message: OWN_DECISION });
    await expect(as(server, ADMIN_TOKEN).post(`/advisor/recommendations/${rec.id}/override`, { option: 'cold', reason: 'The change window fits a cold cutover' })).rejects.toMatchObject({
      status: 409,
      message: OWN_DECISION,
    });
  });

  it('defaults a policy to a distinct approver and keeps Distinct approver approver-only on a plan', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const spec = { name: 'Four eyes', scope: { provider_ids: [], plan_ids: [] }, decision_types: [], mode: 'always_require_approval', threshold: 0.6 };
    const created = await as(server, ADMIN_TOKEN).post<{ require_distinct_approver: boolean }>('/advisor/policies', spec);
    expect(created.require_distinct_approver).toBe(true);
    const draft = server.plans.find((p) => p.status === 'draft')!;
    await expect(as(server, 'operator').patch(`/plans/${draft.id}`, { require_distinct_approver: false })).rejects.toMatchObject({ status: 403 });
    const patched = await as(server, 'approver').patch<{ require_distinct_approver: boolean }>(`/plans/${draft.id}`, { require_distinct_approver: false });
    expect(patched.require_distinct_approver).toBe(false);
  });

  it('seeds requests and approvals by two people, with comments an operator would write', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const requested = server.migrations.filter((m) => m.cutover_requested);
    expect(requested.length).toBeGreaterThan(3);
    for (const m of requested) {
      expect(m.cutover_requested_by, m.vm.name).toBeTruthy();
      expect(m.approvals.every((a) => a.actor !== m.cutover_requested_by), m.vm.name).toBe(true);
    }
    for (const a of server.migrations.flatMap((m) => m.approvals)) expect((a.comment ?? '').length, a.actor).toBeGreaterThanOrEqual(15);
    // the fixtures' validations ran as the operator: the admin persona may decide what waits
    expect(new Set(server.recommendations.filter((r) => !r.legacy).map((r) => r.requested_by))).toEqual(new Set(['bayu']));
  });
});

describe('mock convergence, Force cutover and data checks, like the API (SDD §5.3–§5.5, ADV-16)', () => {
  const GiB = 2 ** 30;
  function as(server: MockServer, token: string) {
    return new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  }
  const REASON = 'The change window closes in 20 minutes';

  it('says on GET /health that authentication is off: no token is the anonymous admin (SDD §12, §13.1)', async () => {
    const { client } = setup(null);
    expect(await client.get<{ auth_disabled: boolean }>('/health')).toMatchObject({ auth_disabled: true });
    expect(await client.get('/me')).toEqual({ name: 'anonymous', role: 'admin' });
  });

  it('answers GET /health with its demo speed: the mock runs model time at wall speed', async () => {
    const { client } = setup('viewer');
    expect(await client.get<{ demo: boolean; demo_speed: number | null }>('/health')).toMatchObject({ demo: true, demo_speed: 1 });
  });

  it('records a Force cutover with the last pass and the estimate, and a request that is no approval', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    expect([m.phase, m.strategy, m.converged_by, m.force_cutover]).toEqual(['syncing', 'warm', null, null]);
    // the API's figure (SDD §5.4): the downtime after the running pass, as the dialog shows it - not the converged estimate
    const expected = forcedDowntime(m, server.plans.find((p) => p.id === m.plan_id)!);
    expect(Math.abs(expected - m.estimate!.downtime_s)).toBeGreaterThan(60);
    const forced = await as(server, 'approver').post<Migration>(`/migrations/${m.id}/force-cutover`, { reason: `  ${REASON}  ` });
    expect(forced.force_cutover).toEqual({
      actor: 'sari',
      at: new Date(NOW).toISOString(),
      reason: REASON,
      delta_bytes: Math.round(12.6 * GiB),
      estimated_downtime_s: expected,
    });
    // maker-checker (SDD §5.4, ADV-17): the forcer requested the cutover; another approver approves it
    expect([forced.cutover_requested, forced.cutover_requested_by, forced.approvals]).toEqual([true, 'sari', []]);
    expect(server.events.at(-1)).toMatchObject({
      kind: 'migration.force_cutover',
      actor: 'sari',
      migration_id: m.id,
      data: { reason: REASON, delta_bytes: Math.round(12.6 * GiB), estimated_downtime_s: expected },
    });
    // the running pass finishes, then the migration waits for the gate with the rule recorded
    for (let i = 0; i < 200 && m.phase === 'syncing'; i += 1) server.tick();
    expect([m.phase, m.converged_by]).toEqual(['awaiting_cutover', 'force']);
    expect(m.sync_passes.at(-1)!.kind).toBe('delta');
  });

  it('records a Force cutover as an approval too when the plan needs no distinct approver', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    server.plans.find((p) => p.id === m.plan_id)!.require_distinct_approver = false;
    const forced = await as(server, 'approver').post<Migration>(`/migrations/${m.id}/force-cutover`, { reason: REASON });
    expect(forced.approvals).toEqual([{ actor: 'sari', at: new Date(NOW).toISOString(), comment: REASON }]);
  });

  it('forces a migration once, like the API (SDD §5.4)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!; // syncing
    await as(server, 'approver').post(`/migrations/${m.id}/force-cutover`, { reason: REASON });
    const first = m.force_cutover;
    // rina, another approver, would take sari's place: sari's own approval would count again
    await expect(as(server, 'admin').post(`/migrations/${m.id}/force-cutover`, { reason: 'I would rather force it myself' })).rejects.toMatchObject({
      status: 409,
      message: 'Already forced by sari: the cutover follows the running pass.',
    });
    expect(m.force_cutover).toEqual(first);
    expect(server.events.filter((e) => e.migration_id === m.id && e.kind === 'migration.force_cutover')).toHaveLength(1);
  });

  it('refuses what the API refuses: a short reason, a converged or single-shot migration, an operator', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const syncing = 'mig-5d7e2b4a11';
    await expect(as(server, 'approver').post(`/migrations/${syncing}/force-cutover`, { reason: ' too short ' })).rejects.toMatchObject({ status: 422 });
    await expect(as(server, 'approver').post(`/migrations/${syncing}/force-cutover`, { reason: 'x'.repeat(2001) })).rejects.toMatchObject({ status: 422 });
    await expect(as(server, 'operator').post(`/migrations/${syncing}/force-cutover`, { reason: REASON })).rejects.toMatchObject({ status: 403 });
    for (const id of ['mig-5d7e2b4a12', 'mig-5d7e2b4a13']) {
      await expect(as(server, 'approver').post(`/migrations/${id}/force-cutover`, { reason: REASON })).rejects.toMatchObject({
        status: 409,
        message: expect.stringMatching(/^a Force cutover applies to a warm migration in pre-copy or syncing, not to (warm in awaiting_cutover|cold in ready)$/),
      });
    }
    expect(server.migrations.find((x) => x.id === syncing)!.force_cutover).toBeNull();
  });

  it('records the rule that ended the passes: the threshold, or the pass limit (SDD §5.3)', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    for (let i = 0; i < 400 && m.phase === 'syncing'; i += 1) server.tick();
    expect(m.phase).toBe('awaiting_cutover');
    const last = m.sync_passes.at(-1)!;
    expect(m.converged_by).toBe(last.bytes_changed <= plan.convergence_threshold_bytes ? 'threshold' : 'max_passes');

    const limited = new MockServer({ now: () => NOW, seed: 1 });
    const other = limited.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    limited.plans.find((p) => p.id === other.plan_id)!.convergence_threshold_bytes = 0;
    for (let i = 0; i < 400 && other.phase === 'syncing'; i += 1) limited.tick();
    expect([other.phase, other.converged_by]).toEqual(['awaiting_cutover', 'max_passes']);
  });

  it('never ends CBT passes on the threshold: the pass limit decides in the mock, which has no SLO rule (SDD §5.4)', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-9e4f6a8b50')!;
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    expect([m.strategy, m.phase]).toEqual(['vmware_warm', 'syncing']);
    for (let i = 0; i < 600 && m.phase === 'syncing'; i += 1) server.tick();
    expect([m.phase, m.converged_by]).toEqual(['awaiting_cutover', 'max_passes']);
    // a pass below the threshold did not end them
    expect(m.sync_passes.slice(0, -1).some((p) => p.kind === 'delta' && p.bytes_changed <= plan.convergence_threshold_bytes)).toBe(true);
  });

  it('clears a Force cutover with the request on re-validation and a strategy change, like the API (SDD §5.4)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    await as(server, 'approver').post(`/migrations/${m.id}/force-cutover`, { reason: REASON });
    // the API clears it with the request it belongs to (SDD §5.4): a strategy change is the operator's way
    m.phase = 'ready';
    await as(server, 'operator').put(`/migrations/${m.id}/strategy`, { strategy: 'cold' });
    expect([m.force_cutover, m.cutover_requested]).toEqual([null, false]);
  });

  it('leaves migrations whose numbers do not hold out of the downtime figures and counts them (SDD §5.5)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const client = as(server, 'viewer');
    const before = await client.get<Stats>('/stats?plan_id=plan-4f2a9c1e');
    expect(before.data_inconsistent).toBe(0);
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e21')!;
    m.data_issues = ['DOWNTIME_BELOW_FINAL_SYNC: 6 s of downtime around a 258 s final sync'];
    m.actual_downtime_s = 99_999;
    const after = await client.get<Stats>('/stats?plan_id=plan-4f2a9c1e');
    expect(after.data_inconsistent).toBe(1);
    expect(after.max_downtime_s ?? 0).toBeLessThan(99_999);
    // still counted as completed: only its downtime is left out
    expect(after.completed).toBe(before.completed);
  });

  it('records the model seconds of a cutover, at wall speed, and counts the downtime from them like the API (SDD §5.2)', () => {
    let clock = NOW;
    const server = new MockServer({ now: () => clock, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-3c1a0f9e23')!;
    expect(m.phase).toBe('cutover');
    for (let i = 0; i < 200 && m.phase !== 'completed'; i += 1) {
      clock += 1000;
      server.tick();
    }
    expect(m.phase).toBe('completed');
    expect(m.downtime_model_s).not.toBeNull();
    expect(m.actual_downtime_s!).toBeGreaterThanOrEqual(m.downtime_model_s!);
    expect(m.data_issues).toEqual([]);
  });
});

describe('mock application probes, like the API (SDD §7.5, §9.3, §9.6, ADV-18)', () => {
  function as(server: MockServer, token: string) {
    return new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  }
  const tcp = (port: number) => ({ kind: 'tcp' as const, port, path: '/', expect_status: null });
  const byName = (server: MockServer, name: string) => server.migrations.find((m) => m.vm.name === name)!;

  it('flags database VMs without an application probe at validation and records the suggestion', () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const mysql = byName(server, 'db-mysql-02');
    expect(mysql.findings.find((f) => f.code === 'APP_PROBE_MISSING')).toMatchObject({
      severity: 'warning',
      message: 'db-mysql-02 is a database VM without an application probe: verification would check only that it booted. Suggested: TCP 3306.',
    });
    expect(mysql.suggested_probes).toEqual([tcp(3306)]);
    expect(byName(server, 'analytics-db').suggested_probes).toEqual([]);
    const web = byName(server, 'web-01');
    expect(web.findings.map((f) => f.code)).not.toContain('APP_PROBE_MISSING');
    expect(web.suggested_probes).toEqual([{ kind: 'http', port: 80, path: '/', expect_status: null }]);
  });

  it('refuses the probes the API refuses and keeps those it accepts', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const operator = as(server, 'operator');
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    await expect(
      operator.patch(`/plans/${plan.id}`, { verification: { ...plan.verification, app_probes: [tcp(70000)] } }),
    ).rejects.toMatchObject({ status: 422, message: expect.stringContaining('70000') });
    const db = byName(server, 'analytics-db');
    const saved = await operator.patch<Plan>(`/plans/${plan.id}`, { probe_overrides: { [db.vm.source_id]: [tcp(5432)] } });
    expect(saved.probe_overrides).toEqual({ [db.vm.source_id]: [tcp(5432)] });
    expect(saved.status).toBe('draft');
    // a validation clears the finding of the VM that now has a probe, and suggests anew
    db.suggested_probes = [tcp(1)];
    await operator.post(`/plans/${plan.id}/validate`);
    for (let i = 0; i < 5; i += 1) server.tick();
    expect(byName(server, 'analytics-db').findings.map((f) => f.code)).not.toContain('APP_PROBE_MISSING');
    expect(byName(server, 'analytics-db').suggested_probes).toEqual([]);
  });

  it('records a failed boot check as a failed verification, whatever ports the plan probes', () => {
    for (const ports of [[22], []]) {
      const clock = { t: NOW };
      const server = new MockServer({ now: () => clock.t, seed: 3 });
      const m = byName(server, 'web-02');
      const plan = server.plans.find((p) => p.id === m.plan_id)!;
      plan.verification = { ...plan.verification, tcp_ports: ports };
      // the one verification in ten that fails on its boot checks
      (server as unknown as { rand: () => number }).rand = () => 0.01;
      verifyNow(server, m, clock);
      expect(m.phase, String(ports)).toBe('rolling_back');
      expect(m.verification!.passed, String(ports)).toBe(false);
      const failed = m.verification!.checks.filter((c) => !c.ok && !c.skipped).map((c) => c.name);
      expect(failed, String(ports)).toEqual(ports.length ? ['tcp:22'] : ['console']);
      expect(m.error).toBe(`Verification failed: ${ports.length ? 'tcp:22 (connection refused)' : 'console (no success pattern in the last 200 console lines)'}`);
    }
  });

  it("runs none of a VM's services when it carries the demo fault, like the demo (SDD §7.4)", () => {
    const clock = { t: NOW };
    const server = new MockServer({ now: () => clock.t, seed: 3 });
    const m = byName(server, 'web-02');
    m.vm = { ...m.vm, name: 'orders-db-02', tags: { role: 'postgresql', 'demo-fault': 'app-down' } };
    server.plans.find((p) => p.id === m.plan_id)!.probe_overrides = { [m.vm.source_id]: [tcp(5432)] };
    verifyNow(server, m, clock);
    expect(m.verification!.probes.map((p) => p.result)).toEqual(['failed']);
  });

  function verifyNow(server: MockServer, m: Migration, clock: { t: number }) {
    for (let i = 0; i < 40 && m.phase === 'verifying'; i += 1) {
      clock.t += 1000;
      server.tick();
    }
  }

  it("records the verification with every probe's result; a database without one needs review", () => {
    const clock = { t: NOW };
    const server = new MockServer({ now: () => clock.t, seed: 3 });
    const m = byName(server, 'web-02');
    expect(m.phase).toBe('verifying');
    m.vm = { ...m.vm, name: 'db-web-02', tags: { tier: 'database' } };
    verifyNow(server, m, clock);
    expect(m.phase).toBe('completed');
    expect(m.checkpoint).toBe('verified'); // the API's value (SDD §8.1)
    expect(m.verification).toMatchObject({ passed: true, probes: [] });
    // the plan's boot checks run as before: its TCP ports, then the console (SDD §7.5)
    expect(m.verification!.checks.map((c) => c.name)).toEqual(['server_active', 'ports_up', 'tcp:22', 'console']);
    expect([m.review_required, m.review_reason]).toEqual([true, 'database VM without an application probe: only its boot was verified']);
  });

  it('fails the verification when a probe is refused, and rolls back as the plan says', () => {
    const clock = { t: NOW };
    const server = new MockServer({ now: () => clock.t, seed: 3 });
    const m = byName(server, 'web-02');
    const plan = server.plans.find((p) => p.id === m.plan_id)!;
    plan.probe_overrides = { [m.vm.source_id]: [tcp(80), tcp(9999)] };
    verifyNow(server, m, clock);
    expect(m.phase).toBe('rolling_back');
    expect(m.verification!.passed).toBe(false);
    expect(m.verification!.probes.map((p) => [p.target.split(':').at(-1), p.result])).toEqual([
      ['80', 'passed'],
      ['9999', 'failed'],
    ]);
    expect(m.error).toContain('app:tcp:9999');
  });
});

describe('mock finalize queue, like the API (SDD §8.1, ADV-19)', () => {
  function as(server: MockServer, token: string) {
    return new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  }
  const byName = (server: MockServer, name: string) => server.migrations.find((m) => m.vm.name === name)!;

  it('returns every plan with its derived status and the finalize reminder', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const pilot = server.plans.find((p) => p.id === 'plan-2d5c8b93')!;
    pilot.status = 'validated';
    const viewer = as(server, 'viewer');
    const one = await viewer.get<Plan>(`/plans/${pilot.id}`);
    expect([one.status, one.derived_status, one.finalize_warning_h]).toEqual(['validated', 'completed', 72]);
    const all = await viewer.get<Plan[]>('/plans');
    expect(all.find((p) => p.id === pilot.id)!.derived_status).toBe('completed');
    expect(all.every((p) => typeof p.derived_status === 'string')).toBe(true);
    // derived on every answer, never stored: the plan's last VM awaiting finalize is finalized
    byName(server, 'web-cache-01').phase = 'finalized';
    expect((await viewer.get<Plan>(`/plans/${pilot.id}`)).derived_status).toBe('finalized');
    expect((await viewer.get<Plan[]>('/plans')).find((p) => p.id === pilot.id)!.derived_status).toBe('finalized');
  });

  it('returns every plan with its migrations counted by phase, which a client cannot set (SDD §4, §8.1, PF-02)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const counted = (planId: string) => {
      const counts: Record<string, number> = {};
      for (const m of server.migrations.filter((x) => x.plan_id === planId)) counts[m.phase] = (counts[m.phase] ?? 0) + 1;
      return counts;
    };
    const viewer = as(server, 'viewer');
    const all = await viewer.get<Plan[]>('/plans');
    for (const plan of all) expect(plan.migration_counts).toEqual(counted(plan.id));
    expect(all.some((p) => Object.keys(p.migration_counts).length > 1)).toBe(true);
    // derived on every answer: a migration that moves moves its count
    byName(server, 'web-cache-01').phase = 'finalized';
    const pilot = await viewer.get<Plan>('/plans/plan-2d5c8b93');
    expect(pilot.migration_counts).toEqual(counted('plan-2d5c8b93'));
    // the API's: a PATCH may not carry it
    await expect(as(server, 'operator').patch(`/plans/${pilot.id}`, { migration_counts: { completed: 9 } })).rejects.toMatchObject({ status: 422 });
  });

  it('splits completed into awaiting finalize and finalized', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const stats = await as(server, 'viewer').get<Stats>('/stats?plan_id=plan-2d5c8b93');
    expect([stats.completed, stats.awaiting_finalize, stats.finalized]).toEqual([2, 1, 1]);
  });

  it('finalizes a wave or the plan with one approval, refusing what the API refuses', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = byName(server, 'web-01');
    const wave = plan.waves.find((w) => w.id === web01.wave_id)!;
    const url = `/plans/${plan.id}/finalize`;
    await expect(as(server, 'operator').post(url, { wave_id: null, confirm: plan.name })).rejects.toMatchObject({ status: 403 });
    await expect(as(server, 'approver').post(url, { wave_id: wave.id, confirm: 'nope' })).rejects.toMatchObject({ status: 400 });
    await expect(as(server, 'approver').post(url, { wave_id: 'wave-9', confirm: wave.name })).rejects.toMatchObject({ status: 404 });
    const out = await as(server, 'approver').post<PlanFinalizeResult>(url, { wave_id: wave.id, confirm: wave.name, delete_source: false });
    expect(out).toEqual({ finalized: [web01.id], skipped: [] });
    expect(web01.phase).toBe('finalized');
    const batch = server.events.filter((e) => e.kind === 'plan.finalized').at(-1)!;
    expect(batch).toMatchObject({ actor: 'sari', plan_id: plan.id, data: { wave_id: wave.id, finalized: ['web-01'], skipped: [] } });
    await expect(as(server, 'approver').post(url, { wave_id: wave.id, confirm: wave.name })).rejects.toMatchObject({ status: 409 });
  });

  it('refuses to roll back a completed migration whose finalize began deleting the source, like the API (SDD §8.1)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const web01 = byName(server, 'web-01');
    expect(web01.phase).toBe('completed');
    web01.checkpoint = 'finalizing';
    await expect(as(server, 'operator').post(`/migrations/${web01.id}/rollback`, { reason: 'revert to the source' })).rejects.toMatchObject({
      status: 409,
      message: 'the finalize began deleting the source VM: finalize it again',
    });
    expect(web01.phase).toBe('completed');
  });

  it('refuses to finalize while the source VM runs again, skips it in a bulk finalize, and finishes a finalize that began deleting it, like the API (SDD §8.1)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = byName(server, 'web-01');
    await as(server, 'operator').post(`/demo/vms/${web01.vm.source_id}/power`, { state: 'on' });
    server.tick();
    expect([web01.phase, web01.source_running_at !== null]).toEqual(['completed', true]);
    const reason = 'the source VM runs again: stop it, or roll it back, before finalizing';
    await expect(as(server, 'approver').post(`/migrations/${web01.id}/finalize`, { confirm: web01.vm.name, delete_source: true })).rejects.toMatchObject({
      status: 409,
      message: reason,
    });
    const wave = plan.waves.find((w) => w.id === web01.wave_id)!;
    const out = await as(server, 'approver').post<PlanFinalizeResult>(`/plans/${plan.id}/finalize`, { wave_id: wave.id, confirm: wave.name });
    // the watch's tick moved the wave on: whatever else completed is finalized, web-01 is skipped with the reason
    expect(out.skipped).toEqual([{ migration_id: web01.id, reason }]);
    expect(out.finalized).not.toContain(web01.id);
    expect(web01.phase).toBe('completed');
    // a finalize that began deleting the source is finalized again from its own page; a bulk finalize skips it too
    web01.checkpoint = 'finalizing';
    const again = await as(server, 'approver').post<PlanFinalizeResult>(`/plans/${plan.id}/finalize`, { wave_id: wave.id, confirm: wave.name });
    expect(again.skipped).toEqual([{ migration_id: web01.id, reason }]);
    await as(server, 'approver').post(`/migrations/${web01.id}/finalize`, { confirm: web01.vm.name, delete_source: true });
    expect(web01.phase).toBe('finalized');
  });

  it("refuses to change a plan's providers while one of its migrations is failed, like the API (SDD §12)", async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const failed = server.migrations.find((m) => m.phase === 'failed')!;
    const plan = server.plans.find((p) => p.id === failed.plan_id)!;
    // a failed migration whose cutover stopped its source holds the whole plan; this one ran on
    failed.downtime_started_at = null;
    failed.downtime_ended_at = null;
    if (!['draft', 'validated'].includes(plan.status)) plan.status = 'validated';
    for (const m of server.migrations.filter((x) => x.plan_id === plan.id && x.id !== failed.id)) {
      Object.assign(m, { phase: 'ready', downtime_started_at: null, downtime_ended_at: null });
    }
    const operator = as(server, 'operator');
    await expect(operator.patch(`/plans/${plan.id}`, { destination_provider_id: 'dst-other' })).rejects.toMatchObject({
      status: 409,
      message: `roll back or cancel the failed migration (${failed.vm.name}) before changing the plan's providers: its rollback acts on the clouds its attempt used`,
    });
    await expect(operator.patch(`/plans/${plan.id}`, { source_provider_id: 'src-other' })).rejects.toMatchObject({ status: 409 });
    // the rest stays editable for the retry, and repeating a provider changes nothing
    expect((await operator.patch<Plan>(`/plans/${plan.id}`, { description: 'fix the mapping' })).description).toBe('fix the mapping');
    await operator.patch(`/plans/${plan.id}`, { destination_provider_id: plan.destination_provider_id });
    failed.phase = 'rolled_back';
    expect((await operator.patch<Plan>(`/plans/${plan.id}`, { destination_provider_id: 'dst-other' })).destination_provider_id).toBe('dst-other');
  });

  it('skips a VM whose finalize fails and finalizes the others, like the API (SDD §8.1)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const plan = server.plans.find((p) => p.id === 'plan-4f2a9c1e')!;
    const web01 = byName(server, 'web-01');
    const other = server.migrations.find((m) => m.plan_id === plan.id && m.id !== web01.id && m.phase === 'syncing')!;
    other.phase = 'completed';
    const internals = server as unknown as { migrationAction: (...args: unknown[]) => unknown };
    const real = internals.migrationAction.bind(server);
    internals.migrationAction = (me, m, action, input) => {
      if (action === 'finalize' && (m as Migration).id === web01.id) throw new Error('source cleanup failed: HTTP 409');
      return real(me, m, action, input);
    };
    const out = await as(server, 'approver').post<PlanFinalizeResult>(`/plans/${plan.id}/finalize`, { wave_id: null, confirm: plan.name });
    const reason = 'finalize failed: source cleanup failed: HTTP 409';
    expect(out).toEqual({ finalized: [other.id], skipped: [{ migration_id: web01.id, reason }] });
    expect([web01.phase, other.phase]).toEqual(['completed', 'finalized']);
    const batch = server.events.filter((e) => e.kind === 'plan.finalized').at(-1)!;
    expect(batch.message).toBe(`${plan.name}: 1 finalized by sari, 1 skipped`);
    expect(batch.data).toEqual({ wave_id: null, finalized: [other.vm.name], skipped: [{ vm: 'web-01', migration_id: web01.id, reason }] });
  });

  it('declares the item key jev_classify sends, never the VM\'s id, like the API (SDD §14.2, §13.4)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const res = server.handle('GET', '/advisor/data-sent', new URLSearchParams(), undefined, 'viewer');
    const items = (res.body as { items: Array<{ decision_type: string; fields: Array<{ key: string; label: string }> }> }).items;
    const classify = items.find((i) => i.decision_type === 'workload_tier')!;
    expect(classify.fields.map((f) => f.key)).toEqual(['id', 'name', 'os', 'tags', 'disks', 'flavor']);
    expect(classify.fields[0]!.label).toContain("not the VM's id");
  });

  it('reports an advisor status the API can produce: an available service carries no last error (SDD §14.3)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const status = server.handle('GET', '/advisor/status', new URLSearchParams(), undefined, 'viewer').body as AdvisorStatus;
    // the API clears last_error with each answered call: an error sits only beside "unavailable"
    for (const service of [status.jev, status.memory]) expect([service.available, service.last_error]).toEqual([true, null]);
  });

  it('clears the source-running flag and the demo power at a rollback, like the API (SDD §8.1)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const cache = byName(server, 'web-cache-01');
    await as(server, 'operator').post(`/demo/vms/${cache.vm.source_id}/power`, { state: 'on' });
    server.tick();
    expect(cache.source_running_at).not.toBeNull();
    await as(server, 'operator').post(`/migrations/${cache.id}/rollback`, { reason: 'the application misbehaves on RHOSO' });
    expect(cache.source_running_at).toBeNull();
    // the power the demo set goes with it: completed again (a retry cut it over), the source the cutover
    // stopped is not flagged by the old setting
    cache.phase = 'completed';
    server.tick();
    expect(cache.source_running_at).toBeNull();
  });

  it('leaves the closed downtime clock of a completed migration it rolls back alone, like the API (SDD §5.2)', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const cache = byName(server, 'web-cache-01');
    // completed: verification closed its clock
    const { actual_downtime_s: actual, downtime_ended_at: endedAt } = cache;
    expect([cache.phase, endedAt !== null]).toEqual(['completed', true]);
    const ended = () => server.events.filter((e) => e.migration_id === cache.id && e.kind === 'migration.downtime_ended').length;
    const before = ended();
    await as(server, 'operator').post(`/migrations/${cache.id}/rollback`, { reason: 'the application misbehaves on RHOSO' });
    for (let i = 0; i < 40 && cache.phase !== 'rolled_back'; i++) server.tick();
    expect(cache.phase).toBe('rolled_back');
    expect([cache.actual_downtime_s, cache.downtime_ended_at, ended()]).toEqual([actual, endedAt, before]);
  });

  it('powers a fake source VM on and off, and the next watch flags its completed migration', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const cache = byName(server, 'web-cache-01');
    const url = `/demo/vms/${cache.vm.source_id}/power`;
    await expect(as(server, 'viewer').post(url, { state: 'on' })).rejects.toMatchObject({ status: 403 });
    await expect(as(server, 'operator').post(url, { state: 'sideways' })).rejects.toMatchObject({ status: 422 });
    await expect(as(server, 'operator').post('/demo/vms/os-nope/power', { state: 'on' })).rejects.toMatchObject({ status: 404 });
    // a finished migration's source is stopped
    server.tick();
    expect(cache.source_running_at).toBeNull();
    const on = await as(server, 'operator').post<VMRef>(url, { state: 'on' });
    expect(on.power_state).toBe('running');
    server.tick();
    expect(cache.source_running_at).not.toBeNull();
    expect(server.events.filter((e) => e.kind === 'migration.source_running').map((e) => e.migration_id)).toEqual([cache.id]);
    await as(server, 'operator').post(url, { state: 'off' });
    server.tick();
    expect(cache.source_running_at).toBeNull();
    // stopped again: recorded once, so every view drops the alert (SDD §8.1)
    server.tick();
    const stopped = server.events.filter((e) => e.kind === 'migration.source_stopped');
    expect(stopped.map((e) => [e.migration_id, e.data])).toEqual([[cache.id, { power_state: 'stopped' }]]);
  });

  it('refuses the demo power route outside demo mode, like the API', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    server.health = { ...server.health, demo: false, demo_speed: null };
    await expect(as(server, 'operator').post(`/demo/vms/${byName(server, 'web-cache-01').vm.source_id}/power`, { state: 'on' })).rejects.toMatchObject({ status: 404 });
  });
});

describe('mock evidence, like the API (SDD §14.4, ADV-02)', () => {
  function validating(advice: { option: string | null; confidence: number | null }, minimum: number | null = null) {
    const server = new MockServer({ now: () => NOW, seed: 1, advise: () => advice });
    server.policies.length = 0;
    if (minimum !== null) server.policies.push(strict(minimum));
    const client = new ApiClient({ getToken: () => 'operator', fetchImpl: createMockFetch(server) });
    // the analytics plan: analytics-db ties cold and warm (nothing meets the SLO)
    const plan = server.plans.find((p) => p.id === 'plan-c81d44a0')!;
    const db = server.migrations.find((m) => m.vm.name === 'analytics-db')!;
    return { server, client, plan, db, before: server.recommendations.length };
  }
  function strict(minimum: number): AdvisorPolicy {
    const at = '2026-10-08T00:00:00Z';
    return {
      id: 'pol-strict',
      name: 'Strict',
      scope: { provider_ids: [], plan_ids: [] },
      decision_types: [],
      mode: 'auto_apply_above_threshold',
      threshold: 0.6,
      min_evidence_for_auto_apply: minimum,
      require_override_reason: false,
      require_distinct_approver: true,
      created_at: at,
      updated_at: at,
      updated_by: 'rina',
    };
  }

  it('records the evidence validation held, measured against the recommended option, with what memory knew', async () => {
    const { server, client, plan, db, before } = validating({ option: 'warm', confidence: 0.8 });
    await client.post(`/plans/${plan.id}/validate`);
    const [rec] = server.recommendations.slice(before);
    expect(rec!.evidence_option).toBe('warm');
    expect(rec!.evidence.map((i) => [i.key, i.effect])).toEqual([
      ['downtime_estimate', 'opposes'], // nothing meets the plan's SLO
      ['mappings', 'supports'],
      ['findings', 'opposes'], // NET_MTU_SHRINK and APP_PROBE_MISSING apply to warm
      ['guest_os', 'context'],
      ['disk_total', 'context'],
      ['disk_count', 'context'],
      ['write_rate', 'context'],
      ['cutover_window', 'context'],
      ['source_kind', 'context'],
    ]);
    expect(rec!.evidence.find((i) => i.key === 'disk_total')!.value).toBe(db.vm.disk_bytes);
    const refs = new Set([db.vm.source_id, db.id, plan.id, plan.source_provider_id]);
    expect(rec!.evidence.every((i) => refs.has(i.source.ref_id))).toBe(true);
    // the query "warm rhel9 200-500G" finds the mock memory's two incidents that say "warm"; at most 3
    expect(rec!.similar_incidents.map((i) => i.title)).toEqual(['Warm pass never converged on a busy PostgreSQL VM', 'VMware CBT reset after snapshot consolidation']);
    expect(rec!.reasons).toEqual([]);
  });

  it('never applies on its own a decision below the policy minimum', async () => {
    const { server, client, plan, before } = validating({ option: 'cold', confidence: 0.99 }, 99);
    await client.post(`/plans/${plan.id}/validate`);
    const [rec] = server.recommendations.slice(before);
    // measured against the advisor's option, not the deterministic warm
    expect(rec!.evidence_option).toBe('cold');
    expect([rec!.decision.status, rec!.decision.reason_code, rec!.decision.final_option]).toEqual(['pending_approval', 'INSUFFICIENT_EVIDENCE', null]);
    const bearing = rec!.evidence.filter((i) => i.effect !== 'context').length;
    server.policies[0] = strict(bearing);
    await client.post(`/plans/${plan.id}/validate`);
    const newest = server.recommendations.at(-1)!;
    expect([newest.decision.status, newest.decision.final_option]).toEqual(['auto_applied', 'cold']);
  });

  it('counts the attempts a VM made before, with the strategy they ran', async () => {
    const { server, client, plan, db, before } = validating({ option: 'warm', confidence: 0.8 });
    // SDD §14.4: the attempts' own strategies, not the migration's now - a validation set it to cold after two warm attempts
    db.attempts = 2;
    db.attempt_strategies = ['warm', 'warm'];
    db.strategy = 'cold';
    await client.post(`/plans/${plan.id}/validate`);
    const [rec] = server.recommendations.slice(before);
    const earlier = rec!.evidence.find((i) => i.key === 'earlier_runs')!;
    expect([earlier.value, earlier.effect, earlier.display, earlier.source]).toEqual([2, 'opposes', '2 earlier attempts with Warm migration', { kind: 'run', ref_id: db.id }]);
  });

  it('asks a new policy for three evidence items unless told otherwise, like the API', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const admin = new ApiClient({ getToken: () => 'admin', fetchImpl: createMockFetch(server) });
    const created = await admin.post<AdvisorPolicy>('/advisor/policies', { name: 'Default minimum', mode: 'auto_apply_above_threshold', threshold: 0.7 });
    expect(created.min_evidence_for_auto_apply).toBe(3);
  });

  it('gives every stored fixture recommendation its evidence', () => {
    const server = new MockServer({ now: () => NOW });
    const stored = server.recommendations.filter((r) => !r.legacy);
    expect(stored.length).toBeGreaterThan(0);
    for (const rec of stored) {
      expect(rec.evidence_option, rec.id).not.toBeNull();
      expect(rec.evidence.some((i) => i.key === 'disk_total'), rec.id).toBe(true);
    }
  });
});

describe('mock plans: port_creation_mode (SDD §4.2, §12)', () => {
  it('is nova unless chosen, and refuses any other mode with the API 422', async () => {
    const { client } = setup('operator');
    const body = { name: 'ports', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: [] };
    await expect(client.post('/plans', body)).resolves.toMatchObject({ port_creation_mode: 'nova' });
    await expect(client.post('/plans', { ...body, port_creation_mode: 'neutron' })).resolves.toMatchObject({ port_creation_mode: 'neutron' });
    await expect(client.post('/plans', { ...body, port_creation_mode: 'ovs' })).rejects.toMatchObject({
      status: 422,
      message: "port_creation_mode: Input should be 'nova' or 'neutron'",
    });
  });
});

describe('mock plans: nic_overrides (SDD §4.2, §12)', () => {
  it('stores lower-case MACs with both fields, and refuses what the API refuses with its messages', async () => {
    const { client } = setup('operator');
    const body = { name: 'nics', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: [] };
    const created = await client.post<Plan>('/plans', { ...body, nic_overrides: { 'vm-1': { 'FA:16:3E:00:00:01': { network: 'tenant-web' } } } });
    expect(created.nic_overrides).toEqual({ 'vm-1': { 'fa:16:3e:00:00:01': { network: 'tenant-web', fixed_ips: null } } });
    await expect(client.post('/plans', { ...body, nic_overrides: { 'vm-1': { 'fa:16:3e:00:00:01': { fixed_ips: [] }, 'not-a-mac': { network: 'x' } } } })).rejects.toMatchObject({
      status: 422,
      message:
        "nic_overrides[vm-1][fa:16:3e:00:00:01]: fixed_ips must list at least one address (leave it out to keep the NIC's own); nic_overrides[vm-1][not-a-mac]: not a MAC address",
    });
  });

  it("stores addresses in the API's canonical form and finds a repeat however it is written", async () => {
    const { client } = setup('operator');
    const body = { name: 'nics', source_provider_id: 'rhosp17-dc1', destination_provider_id: 'rhoso-prod', vm_ids: [] };
    const created = await client.post<Plan>('/plans', { ...body, nic_overrides: { 'vm-1': { 'fa:16:3e:00:00:01': { fixed_ips: ['FD00:0::0009', '10.20.1.9'] } } } });
    expect(created.nic_overrides['vm-1']?.['fa:16:3e:00:00:01']?.fixed_ips).toEqual(['fd00::9', '10.20.1.9']);
    await expect(client.post('/plans', { ...body, nic_overrides: { 'vm-1': { 'fa:16:3e:00:00:01': { fixed_ips: ['fd00::5', 'FD00:0::0005', '10.20.1.300'] } } } })).rejects.toMatchObject({
      status: 422,
      message: 'nic_overrides[vm-1][fa:16:3e:00:00:01]: not an IP address: 10.20.1.300; nic_overrides[vm-1][fa:16:3e:00:00:01]: fd00::5 given twice',
    });
  });
});

describe('providers of community destinations, like the API (SDD §4.2, §10, §10.1, §12; plan T8)', () => {
  const call = (server: MockServer, method: string, path: string, body?: unknown) => server.handle(method, path, new URLSearchParams(), body, 'admin');
  const keysOf = (o: object) => Object.keys(o).sort();
  const base = { endpoint: 'https://keystone.example.org/v3', cloud: 'x' };

  it('the mock check reports the same keys', () => {
    const { server } = setup('admin');
    expect(call(server, 'POST', '/providers', { ...base, id: 'src-x', name: 'Source X', kind: 'openstack', role: 'source', distribution: 'openstack_community' }).status).toBe(201);
    const declared = { ...base, id: 'dc3', name: 'Hibiscus DC3', kind: 'openstack', role: 'destination', distribution: 'kolla', release: '2026.2', policy_profile: 'legacy' };
    expect(call(server, 'POST', '/providers', declared).status).toBe(201);
    expect(call(server, 'POST', '/providers', { ...base, id: 'rhoso-x', name: 'RHOSO X', kind: 'rhoso', role: 'destination', distribution: 'rhoso' }).status).toBe(201);
    const source = (call(server, 'POST', '/providers/src-x/check').body as Provider).capabilities;
    expect(keysOf(source)).toEqual([...CHECK_KEYS.openstack].sort());
    const dc3 = (call(server, 'POST', '/providers/dc3/check').body as Provider).capabilities;
    expect(keysOf(dc3)).toEqual([...CHECK_KEYS.openstack, ...CHECK_KEYS.destination].sort());
    // its series by its maxima, and the profile it declares (SDD §10.1)
    expect(dc3).toMatchObject({ compute_microversion: '2.104', release_range: ['2026.2'], release: '2026.2', support_tier: { tier: 'tier1' }, policy_profile: 'legacy' });
    const rhoso = (call(server, 'POST', '/providers/rhoso-x/check').body as Provider).capabilities;
    expect(rhoso).toMatchObject({ support_tier: { tier: 'validated', reason: null }, policy_profile: 'legacy', tls: true, plain_http: [] });
    // the seeded providers a check reported on carry them too
    const checked = server.providers.filter((p) => p.kind !== 'vmware' && Object.keys(p.capabilities).length);
    expect(checked.map((p) => p.id)).toEqual(expect.arrayContaining(['rhosp17-dc1', 'rhoso-prod', 'hibiscus-dc3']));
    for (const p of checked) expect(keysOf(p.capabilities)).toEqual([...CHECK_KEYS.openstack, ...(p.role === 'destination' ? CHECK_KEYS.destination : [])].sort());
  });

  it("a check after new credentials reports the pools the provider's cloud has, as the API demo does (review RV19-02)", () => {
    const { server } = setup('admin');
    const rhoso = () => server.providers.find((p) => p.id === 'rhoso-prod')!;
    const seeded = rhoso().capabilities.storage_backends;
    // the analytics plan's Spark nodes, handed over to rhoso-prod's pools (SDD §7.3.1)
    const spark = () =>
      server.migrations
        .filter((m) => m.plan_id === 'plan-c81d44a0' && m.vm.name !== 'analytics-db')
        .map((m) => [m.vm.name, m.estimates.find((e) => e.strategy === 'storage_handover')?.reasons ?? ['no estimate']] as const);
    // new credentials reset the report (SDD §12); the check reads the cloud again, whose pools did not change
    expect(call(server, 'PUT', '/providers/rhoso-prod/credentials', { username: 'u', password: 'p', project_name: 'x' })).toMatchObject({
      status: 200,
      body: { status: 'unknown', capabilities: {} },
    });
    expect(call(server, 'POST', '/providers/rhoso-prod/check').status).toBe(200);
    expect(rhoso().capabilities.storage_backends).toEqual(seeded);
    expect(call(server, 'POST', '/plans/plan-c81d44a0/validate').status).toBe(200);
    expect(spark().length).toBeGreaterThan(0);
    expect(spark().filter(([, reasons]) => reasons.length)).toEqual([]);
    // the source keeps its own report too: its pools, and OVN off
    expect(call(server, 'PUT', '/providers/rhosp17-dc1/credentials', { username: 'u', password: 'p', project_name: 'x' }).status).toBe(200);
    expect(call(server, 'POST', '/providers/rhosp17-dc1/check').status).toBe(200);
    const source = server.providers.find((p) => p.id === 'rhosp17-dc1')!.capabilities;
    expect(source).toMatchObject({ ovn: false });
    expect((source.storage_backends as { pool: string }[]).map((b) => b.pool)).toContain('overcloud@tripleo_netapp#192.0.2.60:/cinder_dc1');
  });

  it('takes a declared series and policy profile, and refuses them where the API does (SDD §4.2, §12)', () => {
    const { server } = setup('admin');
    const dst = { ...base, id: 'dc4', name: 'DC4', kind: 'openstack', role: 'destination', distribution: 'openstack_community' };
    expect(call(server, 'POST', '/providers', { ...dst, release: '2026.3' })).toMatchObject({ status: 422 });
    expect(call(server, 'POST', '/providers', { ...dst, policy_profile: 'unknown' })).toMatchObject({ status: 422 });
    const vcenter = { ...base, id: 'vc-9', name: 'vCenter 9', kind: 'vmware', role: 'source', distribution: 'vmware' };
    expect(call(server, 'POST', '/providers', { ...vcenter, release: '2026.2' })).toMatchObject({ status: 422 });
    expect(call(server, 'POST', '/providers', dst)).toMatchObject({ status: 201, body: { release: null, policy_profile: null } });
    // a declaration is a provider change: the check report is reset (SDD §12)
    expect(call(server, 'POST', '/providers/dc4/check').status).toBe(200);
    const patched = call(server, 'PATCH', '/providers/dc4', { release: '2025.2', policy_profile: 'secure_rbac' });
    expect(patched).toMatchObject({ status: 200, body: { release: '2025.2', policy_profile: 'secure_rbac', status: 'unknown', capabilities: {} } });
    expect(call(server, 'PATCH', '/providers/dc4', { release: '2025' })).toMatchObject({ status: 422 });
    expect(call(server, 'PATCH', '/providers/dc4', { release: null, policy_profile: null })).toMatchObject({ status: 200, body: { release: null, policy_profile: null } });
    expect(call(server, 'POST', '/providers', vcenter).status).toBe(201);
    expect(call(server, 'PATCH', '/providers/vc-9', { policy_profile: 'legacy' })).toMatchObject({ status: 422 });
  });
});

