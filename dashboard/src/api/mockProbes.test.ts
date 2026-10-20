import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §9.6).
import raw from '../../../seamless/tests/fixtures/probe_suggestion_cases.json?raw';
import type { AppProbe, GpuDevice, VMRef } from './types';
import { buildFixtures } from './mockData';
import { isDatabase, suggestProbes } from './mockProbes';

const fixture = JSON.parse(raw) as {
  cases: Array<{ name: string; tags: Record<string, string>; os_type: string; gpus?: GpuDevice[]; probes: AppProbe[] }>;
};

const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0]!.vm;

function vm(name: string, tags: Record<string, string>, os_type: string, gpus: GpuDevice[] = []): VMRef {
  return { ...base, name, tags, os_type, flavor_extra_specs: {}, gpus };
}

describe('probe suggestions in the mock, like the control plane (SDD §9.6, ADV-18)', () => {
  it.each(fixture.cases.map((c) => [c.name, c] as const))('suggests for %s what the rules suggest', (_name, c) => {
    expect(suggestProbes(vm(c.name, c.tags, c.os_type, c.gpus))).toEqual(c.probes);
  });

  it('knows a database VM by the tier the rules give it', () => {
    expect(isDatabase(vm('dc2-db-01', {}, 'rhel8_64Guest'))).toBe(true);
    expect(isDatabase(vm('billing-db-01', { role: 'oracle', 'demo-fault': 'app-down' }, 'rhel8'))).toBe(true);
    expect(isDatabase(vm('ledger-web-01', { role: 'frontend' }, 'rhel9'))).toBe(false);
  });
});
