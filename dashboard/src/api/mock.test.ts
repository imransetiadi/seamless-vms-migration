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
} from './types';

const NOW = Date.parse('2026-10-08T12:00:00Z');

function setup(token: string | null = 'admin') {
  const server = new MockServer({ now: () => NOW, seed: 1 });
  const client = new ApiClient({ getToken: () => token, fetchImpl: createMockFetch(server) });
  return { server, client };
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

  it('numbers persisted events in increasing order', () => {
    const seqs = server.events.map((e) => e.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(new Set(seqs).size).toBe(seqs.length);
  });
});

describe('mock API', () => {
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
    // the second plan has not been validated: its migration (created with the plan) holds nothing
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
});
