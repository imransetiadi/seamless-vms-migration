import { describe, expect, it } from 'vitest';
import { REASON_CODES, type AdvisorNote, type AdvisorRecommendation, type Decision, type ReasonCode } from '../api/types';
import { decisionText, nonStrategyNotes, optionLabel, recommendedText } from './advisor';

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
    legacy: false,
    legacy_text: null,
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
  it('reads a 0.57 recommendation under a 0.60 threshold as Recommended: Warm and Decision: Cold, below threshold', () => {
    const r = rec();
    expect(`Recommended: ${recommendedText(r)}`).toBe('Recommended: Warm · 57% confidence');
    expect(`Decision: ${decisionText(r)}`).toBe('Decision: Cold — below threshold (57% < 60%)');
    expect(`${recommendedText(r)} ${decisionText(r)}`).not.toMatch(/selected/i);
  });

  it.each(REASON_CODES.map((code) => [code] as const))('words the reason code %s from the decision, never as the bare code', (code) => {
    const text = decisionText(rec({}, { reason_code: code, ...BY_CODE[code] }));
    expect(text).toMatch(/ — \S/);
    expect(text).not.toContain(code);
    expect(text).not.toMatch(/selected|undefined|null/i);
  });

  it('words every decision as decide() took it: it never compares the confidence with the threshold itself', () => {
    // a contradiction only decide() could resolve: the dashboard repeats the decision, whatever the numbers say
    const kept = rec({ confidence: 0.91 }, { status: 'below_threshold', reason_code: 'BELOW_THRESHOLD_DEFAULT_KEPT', final_option: 'cold' });
    expect(decisionText(kept)).toMatch(/^Cold — below threshold/);
    const applied = rec({ confidence: 0.31 }, { status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', final_option: 'warm' });
    expect(decisionText(applied)).toBe('Warm — applied automatically by policy global-default');
  });

  it('shows the confidence and the threshold at a precision where they differ, in both lines', () => {
    const r = rec({ confidence: 0.595 });
    expect(decisionText(r)).toBe('Cold — below threshold (59.5% < 60%)');
    expect(recommendedText(r)).toBe('Warm · 59.5% confidence');
  });

  it('says who decided when a person did, and keeps their reason out of the reason line', () => {
    expect(decisionText(rec({}, { reason_code: 'APPROVED_BY_USER', ...BY_CODE.APPROVED_BY_USER }))).toBe('Warm — approved by sari');
    expect(decisionText(rec({}, { reason_code: 'OVERRIDDEN_BY_USER', ...BY_CODE.OVERRIDDEN_BY_USER }))).toBe('Cold — overridden by bayu');
  });

  it('says a pending decision is waiting for approval, and why', () => {
    expect(decisionText(rec({}, { reason_code: 'POLICY_REQUIRES_APPROVAL', ...BY_CODE.POLICY_REQUIRES_APPROVAL }))).toBe('Waiting for approval — policy finance requires approval');
    expect(decisionText(rec({}, { reason_code: 'INSUFFICIENT_EVIDENCE', ...BY_CODE.INSUFFICIENT_EVIDENCE }))).toBe('Waiting for approval — too little evidence to apply it automatically');
  });

  it('tells a fallback without an answer from one whose answer was unusable', () => {
    const fallback = { reason_code: 'ADVISOR_UNAVAILABLE_FALLBACK' as const, ...BY_CODE.ADVISOR_UNAVAILABLE_FALLBACK };
    const off = rec({ option: null, confidence: null, distribution: {} }, fallback, { model: { name: 'rules', version: 'rules', deployment: 'unknown', endpoint_host: null } });
    expect(recommendedText(off)).toBe('nothing — the advisor did not answer');
    expect(decisionText(off)).toBe('Cold — advisor unavailable, decided by rules');
    const escaped = rec({ option: 'ask_user', confidence: 0.7 }, fallback);
    expect(recommendedText(escaped)).toBe('Needs your input · 70% confidence');
    expect(decisionText(escaped)).toBe('Cold — no usable recommendation, decided by rules');
  });

  it('words a legacy entry, which has no threshold, with its confidence alone', () => {
    const legacy = rec({}, { threshold_used: null, policy_id: null, actor: { kind: 'system', id: 'legacy' } }, { legacy: true, legacy_text: 'Jev suggested warm (confidence 0.57, status ok); below the bar, kept cold' });
    expect(decisionText(legacy)).toBe('Cold — below threshold (57% confidence)');
    const applied = rec({}, { final_option: 'warm', status: 'auto_applied', reason_code: 'ABOVE_THRESHOLD_AUTO_APPLY', threshold_used: null, policy_id: null }, { legacy: true });
    expect(decisionText(applied)).toBe('Warm — applied automatically');
    // a note that did not record the deterministic choice: the default was kept, not an approval awaited
    const unknown = rec({}, { final_option: null, threshold_used: null, policy_id: null }, { legacy: true });
    expect(decisionText(unknown)).toBe('The default — below threshold (57% confidence)');
  });

  it('labels strategies and the escape hatches; anything else reads as it is', () => {
    expect(optionLabel('storage_handover')).toBe('Storage handover');
    expect(optionLabel('investigate')).toBe('Needs investigation');
    expect(optionLabel('none')).toBe('Other');
    expect(optionLabel('stateless_web')).toBe('stateless_web');
  });

  it('leaves strategy notes out of note lists: they are listed as recommendations', () => {
    const note = (kind: AdvisorNote['kind']): AdvisorNote => ({ kind, source: 'jev', summary: kind, confidence: null, data: {}, created_at: AT });
    expect(nonStrategyNotes([note('strategy'), note('classification'), note('screen')]).map((n) => n.kind)).toEqual(['classification', 'screen']);
  });
});
