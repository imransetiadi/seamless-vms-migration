import { describe, expect, it } from 'vitest';
import type { AdvisorRecommendation, EvidenceItem } from '../api/types';
import { evidenceHref, evidenceSummary, evidenceTally, shownEvidence } from './evidence';

function item(key: string, effect: EvidenceItem['effect'], weight: number, kind: EvidenceItem['source']['kind'] = 'inventory', ref = 'vm-7'): EvidenceItem {
  return { key, label: key, value: 1, display: key, effect, weight, source: { kind, ref_id: ref } };
}

const rec = { provider_id: 'rhosp17-dc1', decision: { reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY' } } as AdvisorRecommendation;

describe('the evidence card (SDD §16, ADV-02)', () => {
  it('shows the six heaviest items, those that support or oppose first', () => {
    const items = [
      item('a', 'context', 99),
      item('b', 'supports', 10),
      item('c', 'opposes', 50),
      item('d', 'context', 80),
      item('e', 'supports', 70),
      item('f', 'context', 20),
      item('g', 'context', 60),
      item('h', 'opposes', 5),
    ];
    const { shown, rest } = shownEvidence(items);
    expect(shown.map((i) => i.key)).toEqual(['e', 'c', 'b', 'h', 'a', 'd']);
    expect(rest.map((i) => i.key)).toEqual(['g', 'f']);
    expect(evidenceTally(items)).toEqual({ supports: 2, opposes: 2, context: 4 });
  });

  it('links each item to its source', () => {
    expect(evidenceHref(item('disk_total', 'context', 60, 'inventory', 'os-0a14'), rec)).toBe('/inventory/rhosp17-dc1?vm=os-0a14');
    expect(evidenceHref(item('mappings', 'supports', 80, 'preflight', 'mig-1'), rec)).toBe('/migrations/mig-1#findings');
    expect(evidenceHref(item('downtime_estimate', 'supports', 100, 'estimate', 'mig-1'), rec)).toBe('/migrations/mig-1#estimates');
    expect(evidenceHref(item('earlier_runs', 'opposes', 85, 'run', 'mig-2'), rec)).toBe('/migrations/mig-2');
    expect(evidenceHref(item('cutover_window', 'context', 30, 'plan', 'plan-1'), rec)).toBe('/plans/plan-1');
    expect(evidenceHref(item('source_kind', 'context', 20, 'provider', 'rhosp17-dc1'), rec)).toBe('/providers');
    // a VM of a recommendation without its provider has no inventory to open
    expect(evidenceHref(item('disk_total', 'context', 60), { ...rec, provider_id: null })).toBeNull();
  });

  it('sums the evidence up in one line, and says when it was too little', () => {
    const items = [item('a', 'supports', 90), item('b', 'supports', 80), item('c', 'opposes', 70), item('d', 'context', 10)];
    expect(evidenceSummary({ ...rec, evidence: items })).toBe('Evidence: 2 support, 1 opposes');
    expect(evidenceSummary({ ...rec, evidence: [item('a', 'opposes', 1), item('b', 'opposes', 1)] })).toBe('Evidence: 0 support, 2 oppose');
    expect(evidenceSummary({ ...rec, evidence: [], decision: { ...rec.decision, reason_code: 'INSUFFICIENT_EVIDENCE' } } as AdvisorRecommendation)).toBe(
      'Limited evidence: 0 items support or oppose',
    );
    expect(evidenceSummary({ ...rec, evidence: [item('a', 'supports', 1)], decision: { ...rec.decision, reason_code: 'INSUFFICIENT_EVIDENCE' } } as AdvisorRecommendation)).toBe(
      'Limited evidence: 1 item supports or opposes',
    );
    // a legacy entry has none
    expect(evidenceSummary({ ...rec, evidence: [] })).toBeNull();
  });
});
