import { describe, expect, it } from 'vitest';
import type { Provider } from '../api/types';
import { credentialHold, cutoverHolds, ownProject, projectChoices, projectCredentialRows, projectWords } from './projectCredentials';
import { deleteRefusal, plansThatNeedProject } from './providerUse';
import type { Plan } from '../api/types';

// a made-up Keystone project and the API's record of its application credential (SDD §4.2, §10, §27.4)
const PROJECT = 'd4e5f6a7b8c94d0e8f1a2b3c4d5e6f70';
const NOW = Date.parse('2026-10-30T12:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();

function provider(entry: Record<string, unknown> | null, updatedAt = iso(NOW - 48 * 3_600_000)): Provider {
  const check = { status: 'ok', message: null, project_name: 'tenant-c', expires_at: null, checked_at: iso(NOW - 3_600_000) };
  return {
    id: 'rhoso-prod',
    name: 'RHOSO 18.0 — Production',
    capabilities: entry === null ? {} : { projects: { [PROJECT]: { ...check, ...entry } } },
    project_credentials: { [PROJECT]: { secret: 'provider-rhoso-prod.project-fedcba9876543210', updated_at: updatedAt } },
  } as unknown as Provider;
}

describe('the cutover gate rule 6 as the migration page judges it (SDD §5.4, §16)', () => {
  it('holds a credential that expires sooner than 24 hours on, never one exactly 24 hours on or later', () => {
    const at = (ms: number) => credentialHold(provider({ expires_at: iso(NOW + ms) }), PROJECT, NOW);
    expect(at(24 * 3_600_000)).toBeNull();
    expect(at(25 * 3_600_000)).toBeNull();
    expect(at(24 * 3_600_000 - 1)?.why).toMatch(/^expires at .+, within 24 hours$/);
    expect(at(23 * 3_600_000)?.remedy).toBe('An admin writes a new credential for the project, then checks the provider.');
    expect(at(-1)?.why).toMatch(/^expired at /);
    expect(credentialHold(provider({ expires_at: null }), PROJECT, NOW)).toBeNull();
    expect(credentialHold(provider({ expires_at: 'soon' }), PROJECT, NOW)?.why).toBe('its last check read an expiry that is no time');
  });

  it('holds a credential written since its last check, or whose check failed, until a check reads it', () => {
    expect(credentialHold(provider(null), PROJECT, NOW)?.why).toMatch(/^written at .+, not checked since$/);
    const later = provider({}, iso(NOW - 60_000));
    expect(credentialHold(later, PROJECT, NOW)?.why).toMatch(/^written at .+, after its last check$/);
    expect(credentialHold(provider({ checked_at: iso(NOW - 48 * 3_600_000) }), PROJECT, NOW)).toBeNull();
    const failed = credentialHold(provider({ status: 'error', message: 'the sign-in failed: HTTP 401' }), PROJECT, NOW);
    expect(failed).toEqual({ why: 'its last check failed: the sign-in failed: HTTP 401', remedy: 'Check the provider: the cutover starts once a check reads it.' });
  });

  it("judges the plan's projects alone, before the cutover, and names each by its name and id", () => {
    const soon = provider({ expires_at: iso(NOW + 3_600_000) });
    const plan = { source_project: null, destination_project: PROJECT };
    const [hold] = cutoverHolds({ phase: 'awaiting_cutover' }, plan, undefined, soon, NOW);
    expect(hold).toMatchObject({ side: 'destination', project: `tenant-c (${PROJECT})`, provider: 'RHOSO 18.0 — Production' });
    // a plan that names no project, a migration past its gate, a provider not loaded: nothing
    expect(cutoverHolds({ phase: 'awaiting_cutover' }, { source_project: null, destination_project: null }, soon, soon, NOW)).toEqual([]);
    expect(cutoverHolds({ phase: 'cutover' }, plan, undefined, soon, NOW)).toEqual([]);
    expect(cutoverHolds({ phase: 'ready' }, plan, undefined, undefined, NOW)).toEqual([]);
    // a project the provider holds no credential for: no step acts with it (SDD §7.1)
    expect(cutoverHolds({ phase: 'ready' }, { source_project: null, destination_project: 'b'.repeat(32) }, undefined, soon, NOW)).toEqual([]);
    expect(projectWords(PROJECT, { project_name: null })).toBe(PROJECT);
  });
});

describe("a provider's project credentials as the card and the pickers show them (SDD §16, plan task TC8)", () => {
  const OTHER = '0a1b2c3d4e5f60718293a4b5c6d7e8f9';
  it('reads each credential with its check, unless it was written since; in the order of their names', () => {
    const p = provider({});
    expect(projectCredentialRows(p)).toEqual([
      { projectId: PROJECT, project: `tenant-c (${PROJECT})`, updatedAt: p.project_credentials![PROJECT]!.updated_at, check: expect.objectContaining({ status: 'ok' }), status: 'ok' },
    ]);
    // written after its check: its name stays, its facts do not
    const since = projectCredentialRows(provider({}, iso(NOW)))[0]!;
    expect(since).toMatchObject({ project: `tenant-c (${PROJECT})`, check: null, status: 'unchecked' });
    // checked exactly when written: current
    expect(projectCredentialRows(provider({ checked_at: iso(NOW) }, iso(NOW)))[0]!.status).toBe('ok');
    expect(projectCredentialRows(provider({ status: 'error', message: 'refused' }))[0]!.status).toBe('error');
    expect(projectCredentialRows(provider(null))[0]).toMatchObject({ project: PROJECT, status: 'unchecked' });
    const two = provider({});
    two.project_credentials![OTHER] = { secret: 'x', updated_at: iso(NOW) };
    two.capabilities = { ...two.capabilities, projects: { ...(two.capabilities.projects as object), [OTHER]: { status: 'ok', project_name: 'alpha', checked_at: iso(NOW) } } };
    expect(projectCredentialRows(two).map((r) => r.projectId)).toEqual([OTHER, PROJECT]);
  });

  it("offers the provider's own project first, sent as none, then its credentials' projects", () => {
    const p = provider({});
    expect(ownProject(p)).toBeNull();
    expect(projectChoices(p)).toEqual([
      { value: '', label: "Its own credential's project" },
      { value: PROJECT, label: `tenant-c (${PROJECT})` },
    ]);
    p.capabilities = { ...p.capabilities, project: { id: 'own-id', name: 'own', domain: 'Default' } };
    expect(ownProject(p)).toEqual({ id: 'own-id', name: 'own' });
    expect(projectChoices(p)[0]).toEqual({ value: '', label: "Its own credential's project: own (own-id)" });
    p.capabilities = { ...p.capabilities, project: { id: 'own-id', name: ' ' } };
    expect(projectChoices(p)[0]!.label).toBe("Its own credential's project: own-id");
    p.capabilities = { ...p.capabilities, project: { id: '' } };
    expect(ownProject(p)).toBeNull();
  });

  it('names the plans that need a project on its provider side alone', () => {
    const plan = (over: Partial<Plan>) =>
      ({ id: 'plan-1', name: 'Tenant C', status: 'validated', derived_status: 'validated', migration_counts: {}, source_provider_id: 'src', destination_provider_id: 'rhoso-prod', source_project: null, destination_project: null, ...over }) as unknown as Plan;
    const plans = [
      plan({ destination_project: PROJECT }),
      plan({ id: 'plan-2', name: 'Same id, other side', source_project: PROJECT }),
      plan({ id: 'plan-3', name: 'Other project', destination_project: OTHER }),
      plan({ id: 'plan-4', name: 'Done', status: 'completed', derived_status: 'completed', destination_project: PROJECT }),
    ];
    expect(plansThatNeedProject('rhoso-prod', PROJECT, plans).map((u) => u.plan.id)).toEqual(['plan-1']);
    expect(plansThatNeedProject('src', PROJECT, plans).map((u) => u.plan.id)).toEqual(['plan-2']);
    expect(deleteRefusal(plansThatNeedProject('rhoso-prod', PROJECT, plans), 3, 'projects')).toBe(
      'A plan still needs it: “Tenant C” (validated). A plan needs its projects until it completes and its VMs are finalized.',
    );
  });
});
