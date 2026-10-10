/**
 * The wording of advisor recommendations and decisions (SDD §14.4, §16).
 *
 * The control plane's `decide()` takes every decision; this module only puts it into words. It never
 * compares a confidence with a threshold: what a decision says comes from its status and reason code.
 */
import type { AdvisorNote, AdvisorRecommendation, DecisionType, Strategy } from '../api/types';
import { formatPct } from './format';
import { STRATEGY_LABELS } from './status';

/** jev_decide's escape hatches (SDD §14.1): answers that are not one of the options. */
const ESCAPE_HATCH_LABELS: Record<string, string> = {
  ask_user: 'Needs your input',
  investigate: 'Needs investigation',
  // TODO(ADV-03): confirm - open question #1, until answered `none` reads "Other"
  none: 'Other',
};

export const DECISION_TYPE_LABELS: Record<DecisionType, string> = {
  migration_strategy: 'Migration strategy',
  workload_tier: 'Workload tier',
  verification_review: 'Verification review',
  console_screening: 'Console screening',
};

export function optionLabel(option: string): string {
  return STRATEGY_LABELS[option as Strategy] ?? ESCAPE_HATCH_LABELS[option] ?? option;
}

const pct = (value: number, decimals: number) => formatPct(value * 100, decimals);

/** The confidence and the threshold as shown: at a precision where they read differently whenever they differ. */
function shown(rec: AdvisorRecommendation): { confidence: string | null; threshold: string | null } {
  const confidence = rec.recommendation.confidence;
  const threshold = rec.decision.threshold_used;
  let decimals = 0;
  while (confidence !== null && threshold !== null && decimals < 2 && pct(confidence, decimals) === pct(threshold, decimals)) decimals += 1;
  return {
    confidence: confidence === null ? null : pct(confidence, decimals),
    threshold: threshold === null ? null : pct(threshold, decimals),
  };
}

/** What the advisor recommended: "Warm · 57% confidence" (the line reads "Recommended: …"). */
export function recommendedText(rec: AdvisorRecommendation): string {
  const { option } = rec.recommendation;
  if (option === null) return rec.model.name === 'rules' ? 'nothing — the advisor did not answer' : 'nothing — the advisor gave no option';
  const { confidence } = shown(rec);
  return `${optionLabel(option)} · ${confidence === null ? 'no confidence given' : `${confidence} confidence`}`;
}

/** Why the decision is what it is, read from the decision alone. */
function decisionReason(rec: AdvisorRecommendation): string {
  const d = rec.decision;
  const { confidence, threshold } = shown(rec);
  switch (d.reason_code) {
    case 'ABOVE_THRESHOLD_AUTO_APPLY':
      return d.policy_id ? `applied automatically by policy ${d.policy_id}` : 'applied automatically';
    case 'BELOW_THRESHOLD_DEFAULT_KEPT':
      if (confidence === null) return threshold ? `below threshold (no confidence given; threshold ${threshold})` : 'below threshold (no confidence given)';
      return threshold ? `below threshold (${confidence} < ${threshold})` : `below threshold (${confidence} confidence)`;
    case 'ADVISOR_UNAVAILABLE_FALLBACK':
      return rec.model.name === 'rules' ? 'advisor unavailable, decided by rules' : 'no usable recommendation, decided by rules';
    case 'POLICY_REQUIRES_APPROVAL':
      return d.policy_id ? `policy ${d.policy_id} requires approval` : 'the policy requires approval';
    case 'INSUFFICIENT_EVIDENCE':
      return 'too little evidence to apply it automatically';
    case 'APPROVED_BY_USER':
      return `approved by ${d.actor.id}`;
    case 'OVERRIDDEN_BY_USER':
      return `overridden by ${d.actor.id}`;
  }
}

/** What was decided and why: "Cold — below threshold (57% < 60%)" (the line reads "Decision: …"). */
export function decisionText(rec: AdvisorRecommendation): string {
  const { final_option: final, status } = rec.decision;
  // no final option: a decision waiting for approval, or a legacy note that did not record the default
  const shownFinal = final !== null ? optionLabel(final) : status === 'pending_approval' ? 'Waiting for approval' : 'The default';
  return `${shownFinal} — ${decisionReason(rec)}`;
}

/** Strategy notes are listed as (legacy) recommendations (SDD §14.4), so note lists leave them out. */
export function nonStrategyNotes<T extends Pick<AdvisorNote, 'kind'>>(notes: T[]): T[] {
  return notes.filter((n) => n.kind !== 'strategy');
}
