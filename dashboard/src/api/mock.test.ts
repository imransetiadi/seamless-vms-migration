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

  it('honours limit and offset on the migrations list', async () => {
    const { server, client } = setup();
    const all = await client.get<Migration[]>('/migrations');
    expect(all).toHaveLength(server.migrations.length);
    const page = await client.get<Migration[]>('/migrations', { query: { limit: 2, offset: 1 } });
    expect(page.map((m) => m.id)).toEqual(all.slice(1, 3).map((m) => m.id));
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

  it('finalizes only with the typed VM name', async () => {
    const { server, client } = setup('approver');
    const completed = byPhase(server, 'completed');
    await expect(
      client.post(`/migrations/${completed.id}/finalize`, { confirm: 'wrong', delete_source: false }),
    ).rejects.toMatchObject({ status: 400, code: 'confirmation_mismatch' });
    const result = await client.post<Migration>(`/migrations/${completed.id}/finalize`, { confirm: completed.vm.name });
    expect(result.phase).toBe('finalized');
  });

  it('refuses to start a plan with blocked migrations', async () => {
    const { server, client } = setup('operator');
    const blocked = byPhase(server, 'blocked');
    const plan = server.plans.find((p) => p.id === blocked.plan_id);
    if (!plan) throw new Error('plan missing');
    plan.status = 'validated';
    await expect(client.post(`/plans/${plan.id}/start`)).rejects.toMatchObject({ status: 409, code: 'blocked_migrations' });
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
