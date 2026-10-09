import { describe, expect, it } from 'vitest';
import { ApiClient } from './client';
import { createMockFetch, MockServer } from './mock';
import { streamEvents } from './stream';
import {
  ADVISOR_NOTE_KINDS,
  ADVISOR_NOTE_SOURCES,
  PHASES,
  PLAN_STATUSES,
  PROVIDER_STATUSES,
  SEVERITIES,
  STRATEGIES,
  type Event,
  type Migration,
  type Plan,
  type Provider,
  type VMRef,
} from './types';

const NOW = Date.parse('2026-10-08T12:00:00Z');

function setup(token: string | null = 'admin') {
  const server = new MockServer({ now: () => NOW, seed: 1 });
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  return { server, client };
}

/** Inventory VMs of `providerId` that no migration of the fixtures uses: free for a new plan. */
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
    for (let t = 0; t < 200 && m.phase === 'cutover'; t++) server.tick();
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
});
