import { describe, expect, it } from 'vitest';
import type { AppProbe, Plan } from '../api/types';
import { describeProbe, probeProblems, probesFor } from './probes';

const tcp = (port: number): AppProbe => ({ kind: 'tcp', port, path: '/', expect_status: null });
const http = (port: number, path = '/', expect_status: number | null = null): AppProbe => ({ kind: 'http', port, path, expect_status });

describe('application probes (SDD §7.5, ADV-18)', () => {
  it('describes a probe the way people say it', () => {
    expect(describeProbe(tcp(5432))).toBe('TCP 5432');
    expect(describeProbe(http(9200))).toBe('HTTP 9200 /');
    expect(describeProbe({ kind: 'https', port: 443, path: '/health', expect_status: 204 })).toBe('HTTPS 443 /health (204)');
  });

  it("runs a VM's override, else the plan's probes, and says which", () => {
    const plan = { verification: { app_probes: [http(80)] }, probe_overrides: { 'vm-db': [tcp(5432)], 'vm-none': [] } } as unknown as Plan;
    expect(probesFor('vm-db', plan)).toEqual({ probes: [tcp(5432)], source: 'override' });
    expect(probesFor('vm-none', plan)).toEqual({ probes: [], source: 'override' });
    expect(probesFor('vm-web', plan)).toEqual({ probes: [http(80)], source: 'plan' });
  });

  it('checks a probe list as the API does (SDD §12)', () => {
    expect(probeProblems([tcp(5432), http(80)])).toEqual([]);
    expect(probeProblems([tcp(0)])).toEqual(['Probe 1: use a port from 1 to 65535.']);
    expect(probeProblems([http(80, 'health')])).toEqual(["Probe 1: the path starts with '/' (at most 200 characters)."]);
    expect(probeProblems([http(80, '/', 99)])).toEqual(['Probe 1: the expected status is from 100 to 599.']);
    // a TCP probe has no path or status to check
    expect(probeProblems([{ kind: 'tcp', port: 22, path: 'x', expect_status: 1 }])).toEqual([]);
    expect(probeProblems(Array.from({ length: 11 }, (_, i) => tcp(1000 + i)))).toEqual(['At most 10 probes for one VM.']);
  });
});
