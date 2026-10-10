import { describe, expect, it } from 'vitest';
// One case table for the control plane and the dashboard's mock (SDD §14.4, ADV-02).
import raw from '../../../seamless/tests/fixtures/evidence_cases.json?raw';
import { buildStrategyEvidence, evidenceCount } from './mockEvidence';
import type { Estimate, EvidenceItem, Finding, Plan, ProviderKind, Strategy, VMRef } from './types';

interface Case {
  name: string;
  vm: VMRef;
  plan: Pick<Plan, 'id' | 'name' | 'source_provider_id' | 'downtime_slo_s' | 'cutover_window'>;
  provider_kind: ProviderKind;
  estimates: Estimate[];
  findings: Finding[];
  option: Strategy;
  earlier: { id: string; strategy: Strategy; attempts: number } | null;
  migration_id: string;
  expected: EvidenceItem[];
}

const cases = JSON.parse(raw) as Case[];

describe('the evidence of a strategy recommendation in the mock, like the control plane (SDD §14.4, ADV-02)', () => {
  it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const items = buildStrategyEvidence(c.vm, c.plan, c.provider_kind, c.estimates, c.findings, c.option, c.earlier, c.migration_id);
    expect(items).toEqual(c.expected);
  });

  it('counts what supports or opposes, never context', () => {
    const [first] = cases;
    expect(evidenceCount(first!.expected)).toBe(first!.expected.filter((i) => i.effect !== 'context').length);
    expect(evidenceCount(first!.expected)).toBe(5);
  });
});
