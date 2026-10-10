import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { canonicalJson, sha256Hex } from '../lib/canonical';
import { createMockFetch, MockServer } from './mock';
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
  type AdvisorPolicy,
  type AdvisorRecommendation,
  type Event,
  type Migration,
  type Plan,
  type Provider,
  type PlanFinalizeResult,
  type RecommendationPage,
  type Stats,
  type VMRef,
} from './types';

const NOW = Date.parse('2026-10-08T12:00:00Z');

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

    // the next cutover counts from the first stop
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
      message: expect.stringContaining(`${held.vm.name} (plan "${first.name}", ${held.phase})`),
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
    await expect(client.post(`/plans/${third.id}/validate`)).rejects.toMatchObject({
      status: 409,
      message: `2 VM(s) already have a migration in another plan: ${a} (plan "${first.name}", ready), ${b} (plan "${first.name}", ready); finish, roll back or cancel it there, or remove the VM from vm_ids`,
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
    // a rolled-back VM lets it go: a retry refuses when the provider is gone (SDD §5.1)
    first!.phase = 'rolled_back';
    second!.phase = 'cancelled';
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
    expect(server.events.filter((e) => e.data.target_id === id).at(-1)).toMatchObject({ kind: 'decision.overridden', actor: 'user:rina', data: { before: { status: 'pending_approval' } } });
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
      after: { status: 'auto_applied', final_option: 'cold', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', policy_id: 'global-default', threshold_used: 0.6 },
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

  it('answers GET /health with its demo speed: the mock runs model time at wall speed', async () => {
    const { client } = setup('viewer');
    expect(await client.get<{ demo: boolean; demo_speed: number | null }>('/health')).toMatchObject({ demo: true, demo_speed: 1 });
  });

  it('records a Force cutover with the last pass and the estimate, and a request that is no approval', async () => {
    const server = new MockServer({ now: () => NOW, seed: 1 });
    const m = server.migrations.find((x) => x.id === 'mig-5d7e2b4a11')!;
    expect([m.phase, m.strategy, m.converged_by, m.force_cutover]).toEqual(['syncing', 'warm', null, null]);
    const forced = await as(server, 'approver').post<Migration>(`/migrations/${m.id}/force-cutover`, { reason: `  ${REASON}  ` });
    expect(forced.force_cutover).toEqual({
      actor: 'sari',
      at: new Date(NOW).toISOString(),
      reason: REASON,
      delta_bytes: Math.round(12.6 * GiB),
      estimated_downtime_s: m.estimate!.downtime_s,
    });
    // maker-checker (SDD §5.4, ADV-17): the forcer requested the cutover; another approver approves it
    expect([forced.cutover_requested, forced.cutover_requested_by, forced.approvals]).toEqual([true, 'sari', []]);
    expect(server.events.at(-1)).toMatchObject({
      kind: 'migration.force_cutover',
      actor: 'sari',
      migration_id: m.id,
      data: { reason: REASON, delta_bytes: Math.round(12.6 * GiB), estimated_downtime_s: m.estimate!.downtime_s },
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
    db.attempts = 2;
    db.strategy = 'warm';
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
