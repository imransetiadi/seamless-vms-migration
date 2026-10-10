import { describe, expect, it } from 'vitest';
import { REASON_CODES, type AdvisorNote, type AdvisorRecommendation, type Decision, type ReasonCode } from '../api/types';
import { buildFixtures } from '../api/mockData';
import { decisionText, distributionLabel, distributionRows, nonStrategyNotes, optionLabel, recommendedText, waitingDecision } from './advisor';

const AT = '2026-10-10T08:00:00Z';

function rec(
  recommendation: Partial<AdvisorRecommendation['recommendation']> = {},
  decision: Partial<Decision> = {},
  extra: Partial<AdvisorRecommendation> = {},
): AdvisorRecommendation {
  return {
    id: 'rec-000000000001',
    created_at: AT,
    decision_type: 'migration_strategy',
    subject: { kind: 'vm', id: 'os-0e65', name: 'static-cdn-01' },
    plan_id: 'plan-2d5c8b93',
    migration_id: 'mig-d4e6f8a090',
    provider_id: 'rhosp17-dc1',
    model: { name: 'jev', version: 'jev-1.13.0', deployment: 'unknown', endpoint_host: null },
    recommendation: { option: 'warm', confidence: 0.57, distribution: { warm: 0.57, cold: 0.43 }, ...recommendation },
    decision: {
      final_option: 'cold',
      status: 'below_threshold',
      reason_code: 'BELOW_THRESHOLD_DEFAULT_KEPT',
      reason_text: null,
      actor: { kind: 'policy', id: 'global-default' },
      policy_id: 'global-default',
      threshold_used: 0.6,
      decided_at: AT,
      ...decision,
    },
    input_snapshot_sha256: null,
    requested_by: null,
    legacy: false,
    legacy_text: null,
    evidence_option: null,
    evidence: [],
    similar_incidents: [],
    reasons: [],
    ...extra,
  };
}

/** One decision per reason code, as decide() writes it (SDD §14.4). */
const BY_CODE: Record<ReasonCode, Partial<Decision>> = {
  ABOVE_THRESHOLD_AUTO_APPLY: { final_option: 'warm', status: 'auto_applied' },
  POLICY_REQUIRES_APPROVAL: { final_option: null, status: 'pending_approval', policy_id: 'finance' },
  INSUFFICIENT_EVIDENCE: { final_option: null, status: 'pending_approval' },
  APPROVED_BY_USER: { final_option: 'warm', status: 'approved', actor: { kind: 'user', id: 'sari' } },
  OVERRIDDEN_BY_USER: { final_option: 'cold', status: 'overridden', actor: { kind: 'user', id: 'bayu' }, reason_text: 'The change window is too short for warm' },
  BELOW_THRESHOLD_DEFAULT_KEPT: { final_option: 'cold', status: 'below_threshold' },
  ADVISOR_UNAVAILABLE_FALLBACK: { final_option: 'cold', status: 'fallback_rules', actor: { kind: 'system', id: 'fallback' }, policy_id: null, threshold_used: null },
};

describe('advisor recommendation wording (SDD §14.4, §16)', () => {
  it('reads a 0.57 recommendation under a 0.60 threshold as Recommended: Warm and Decision: Cold, below threshold (57% < 60%)', () => {
    const r = rec();
    expect(`Recommended: ${recommendedText(r)}`).toBe('Recommended: Warm migration · 57% confidence');
    expect(`Decision: ${decisionText(r)}`).toBe('Decision: Cold migration — below threshold (57% < 60%), default kept');
    expect(`${recommendedText(r)} ${decisionText(r)}`).not.toMatch(/selected/i);
  });

  it('shows the threshold the decision used once, and the confidence in one format (ADV-03, the review of 10 October)', () => {
    for (const code of REASON_CODES) {
      const r = rec({ confidence: 0.6789 }, { reason_code: code, ...BY_CODE[code] });
      const note = `${recommendedText(r)} ${decisionText(r)}`;
      expect(note.match(/60%/g) ?? [], code).toHaveLength(r.decision.threshold_used !== null ? 1 : 0);
      expect(note, code).toMatch(/67%/);
      expect(note, code).not.toMatch(/0\.67|67\.\d/);
    }
  });

  it.each(REASON_CODES.map((code) => [code] as const))('words the reason code %s from the decision, never as the bare code', (code) => {
    const text = decisionText(rec({ confidence: 0.83 }, { reason_code: code, ...BY_CODE[code] }));
    expect(text).toMatch(/ — \S/);
    expect(text).not.toContain(code);
    expect(text).not.toMatch(/selected|undefined|null/i);
    // numbers only in the comparison a decision taken against the threshold ends with
    const compared = ['BELOW_THRESHOLD_DEFAULT_KEPT', 'ABOVE_THRESHOLD_AUTO_APPLY', 'INSUFFICIENT_EVIDENCE'].includes(code);
    expect(/\(\d+% [<≥] \d+%\)/.test(text), code).toBe(compared);
  });

  it("takes the comparison's sign from the decision: the dashboard compares nothing itself", () => {
    // a contradiction only decide() could resolve: the dashboard repeats the decision, whatever the numbers say
    const kept = rec({ confidence: 0.91 }, { status: 'below_threshold', reason_code: 'BELOW_THRESHOLD_DEFAULT_KEPT', final_option: 'cold' });
    expect(decisionText(kept)).toBe('Cold migration — below threshold (91% < 60%), default kept');
    const applied = rec({ confidence: 0.31 }, { status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', final_option: 'warm' });
    expect(decisionText(applied)).toBe('Warm migration — applied automatically by policy global-default (31% ≥ 60%)');
  });

  it('rounds the confidence down, so a missed threshold never reads as met', () => {
    expect(recommendedText(rec({ confidence: 0.595 }))).toBe('Warm migration · 59% confidence');
    expect(decisionText(rec({ confidence: 0.595 }))).toBe('Cold migration — below threshold (59% < 60%), default kept');
    // 0.57 * 100 is 56.99999999999999 in binary floating point
    expect(recommendedText(rec({ confidence: 0.57 }))).toBe('Warm migration · 57% confidence');
    const exact = rec({ confidence: 0.6 }, { status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', final_option: 'warm' });
    expect(decisionText(exact)).toBe('Warm migration — applied automatically by policy global-default (60% ≥ 60%)');
  });

  it('shows both numbers at the decimals of a threshold that is not a whole percent, so the sign always holds', () => {
    const above = rec({ confidence: 0.6051 }, { status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', final_option: 'warm', threshold_used: 0.605 });
    expect(recommendedText(above)).toBe('Warm migration · 60.5% confidence');
    expect(decisionText(above)).toBe('Warm migration — applied automatically by policy global-default (60.5% ≥ 60.5%)');
    const below = rec({ confidence: 0.6049 }, { threshold_used: 0.605 });
    expect(decisionText(below)).toBe('Cold migration — below threshold (60.4% < 60.5%), default kept');
  });

  it('says who decided when a person did', () => {
    expect(decisionText(rec({}, { reason_code: 'APPROVED_BY_USER', ...BY_CODE.APPROVED_BY_USER }))).toBe('Warm migration — approved by sari');
    expect(decisionText(rec({}, { reason_code: 'OVERRIDDEN_BY_USER', ...BY_CODE.OVERRIDDEN_BY_USER }))).toBe('Cold migration — overridden by bayu');
  });

  it('says a pending decision is waiting for approval, and why; a policy that asks first shows its threshold on the Recommended line', () => {
    const asked = rec({}, { reason_code: 'POLICY_REQUIRES_APPROVAL', ...BY_CODE.POLICY_REQUIRES_APPROVAL });
    expect(decisionText(asked)).toBe('Waiting for approval — policy finance requires approval');
    expect(recommendedText(asked)).toBe('Warm migration · 57% confidence (threshold 60%)');
    const thin = rec({ confidence: 0.83 }, { reason_code: 'INSUFFICIENT_EVIDENCE', ...BY_CODE.INSUFFICIENT_EVIDENCE });
    expect(decisionText(thin)).toBe('Waiting for approval — too little evidence to apply it automatically (83% ≥ 60%)');
    expect(recommendedText(thin)).toBe('Warm migration · 83% confidence');
  });

  it('tells a fallback without an answer from one whose answer was unusable', () => {
    const fallback = { reason_code: 'ADVISOR_UNAVAILABLE_FALLBACK' as const, ...BY_CODE.ADVISOR_UNAVAILABLE_FALLBACK };
    const off = rec({ option: null, confidence: null, distribution: {} }, fallback, { model: { name: 'rules', version: 'rules', deployment: 'unknown', endpoint_host: null } });
    expect(recommendedText(off)).toBe('nothing — the advisor did not answer');
    expect(decisionText(off)).toBe('Cold migration — advisor unavailable, decided by rules');
    const escaped = rec({ option: 'ask_user', confidence: 0.7 }, fallback);
    expect(recommendedText(escaped)).toBe('Needs your input · 70% confidence');
    expect(decisionText(escaped)).toBe('Cold migration — no usable recommendation, decided by rules');
  });

  it('words a legacy entry, which has no threshold, with its confidence alone', () => {
    const legacy = rec({}, { threshold_used: null, policy_id: null, actor: { kind: 'system', id: 'legacy' } }, { legacy: true, legacy_text: 'Jev suggested warm (confidence 0.57, status ok); below the bar, kept cold' });
    expect(recommendedText(legacy)).toBe('Warm migration · 57% confidence');
    expect(decisionText(legacy)).toBe('Cold migration — below threshold, default kept');
    const applied = rec({}, { final_option: 'warm', status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', threshold_used: null, policy_id: null }, { legacy: true });
    expect(decisionText(applied)).toBe('Warm migration — applied automatically');
    // a note that did not record the deterministic choice: the default was kept, not an approval awaited
    const unknown = rec({}, { final_option: null, threshold_used: null, policy_id: null }, { legacy: true });
    expect(decisionText(unknown)).toBe('Not recorded — below threshold, default kept');
  });

  it("labels options from the spec's table, CBT only where the strategy uses it, and never shows an internal id in a note", () => {
    expect(['cold', 'warm', 'storage_handover', 'vmware_cold', 'vmware_warm', 'ask_user', 'investigate'].map(optionLabel)).toEqual([
      'Cold migration',
      'Warm migration',
      'Storage handover',
      'VMware cold migration',
      'VMware warm migration (CBT)',
      'Needs your input',
      'Needs investigation',
    ]);
    // `none` has no settled meaning (open question #1): a note says so, the Details tab reads "Other"
    expect(optionLabel('none')).toBe('no clear option');
    expect(distributionLabel('none')).toBe('Other');
    expect(distributionLabel('warm')).toBe('Warm migration');
    expect(optionLabel('stateless_web')).toBe('stateless_web');
    for (const option of ['ask_user', 'investigate', 'none', 'storage_handover', 'vmware_warm']) {
      const note = `${recommendedText(rec({ option }))} ${decisionText(rec({ option }, { final_option: option, status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY' }))}`;
      expect(note, option).not.toMatch(/\b(ask_user|investigate|none|storage_handover|vmware_warm|status ok)\b/);
    }
  });

  it('lists the distribution highest first, with the Details labels', () => {
    const r = rec({ distribution: { cold: 0.2, none: 0.05, warm: 0.75 } });
    expect(distributionRows(r)).toEqual([
      { option: 'warm', label: 'Warm migration', pct: '75%' },
      { option: 'cold', label: 'Cold migration', pct: '20%' },
      { option: 'none', label: 'Other', pct: '5%' },
    ]);
  });

  it('leaves strategy notes out of note lists: they are listed as recommendations', () => {
    const note = (kind: AdvisorNote['kind']): AdvisorNote => ({ kind, source: 'jev', summary: kind, confidence: null, data: {}, created_at: AT });
    expect(nonStrategyNotes([note('strategy'), note('classification'), note('screen')]).map((n) => n.kind)).toEqual(['classification', 'screen']);
  });
});

describe('waitingDecision (SDD §14.6)', () => {
  it('names the held recommendation only while the migration can take the decision', () => {
    const base = buildFixtures(Date.parse('2026-10-08T12:00:00Z')).migrations[0]!;
    for (const phase of ['pending', 'blocked', 'ready'] as const) {
      expect(waitingDecision({ ...base, phase, advisor_hold: 'rec-1' }), phase).toBe('rec-1');
    }
    for (const phase of ['cancelled', 'precopy', 'completed', 'failed'] as const) {
      expect(waitingDecision({ ...base, phase, advisor_hold: 'rec-1' }), phase).toBeNull();
    }
    expect(waitingDecision({ ...base, phase: 'ready', advisor_hold: null })).toBeNull();
  });
});
