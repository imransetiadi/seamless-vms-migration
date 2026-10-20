/**
 * The wording of advisor recommendations and decisions (SDD §14.4, §16).
 *
 * The control plane's `decide()` takes every decision; this module only puts it into words. It never
 * compares a confidence with a threshold: what a decision says comes from its status and reason code.
 */
import type { AdvisorNote, AdvisorRecommendation, DecisionType, Migration } from '../api/types';
import { formatPct } from './format';
import { PERMISSIONS } from './roles';

/**
 * The spec's option labels (ADV-03), "(CBT)" only where the strategy uses changed-block tracking: the
 * OpenStack warm strategy pre-copies snapshots and scans the whole device (SDD §9.2, §16).
 */
const OPTION_LABELS: Record<string, string> = {
  cold: 'Cold migration',
  warm: 'Warm migration',
  storage_handover: 'Storage handover',
  vmware_cold: 'VMware cold migration',
  vmware_warm: 'VMware warm migration (CBT)',
  // jev_decide's escape hatches (SDD §14.1)
  ask_user: 'Needs your input',
  investigate: 'Needs investigation',
};

/** Where the model that answered runs: "On-prem", "Remote: {host}", "Remote", or null when unknown (SDD §13.4, §16). */
export function deploymentLabel(deployment: 'on_prem' | 'remote' | 'unknown', host: string | null): string | null {
  if (deployment === 'on_prem') return 'On-prem';
  if (deployment === 'remote') return host ? `Remote: ${host}` : 'Remote';
  return null;
}

/** One sentence, on the word "confidence" (open question #2: Jev's own figure is kept as it is). */
export const CONFIDENCE_DEFINITION = "Jev's calibrated confidence in its own recommendation, not the probability it gives that option.";

export const DECISION_TYPE_LABELS: Record<DecisionType, string> = {
  migration_strategy: 'Migration strategy',
  workload_tier: 'Workload tier',
  verification_review: 'Verification review',
  console_screening: 'Console screening',
};

/** An option in a note's lines. `none` has no settled meaning yet (TODO(ADV-03): confirm - open question #1). */
export function optionLabel(option: string): string {
  return option === 'none' ? 'no clear option' : (OPTION_LABELS[option] ?? option);
}

/** An option in the Details distribution, where `none` reads "Other". */
export function distributionLabel(option: string): string {
  return option === 'none' ? 'Other' : (OPTION_LABELS[option] ?? option);
}

/** The decimals a threshold needs in percent: 0 for a whole percent, else 1 or 2 (SDD §16). */
export function thresholdDecimals(threshold: number): number {
  const pct = threshold * 100;
  return [0, 1, 2].find((d) => Math.abs(pct * 10 ** d - Math.round(pct * 10 ** d)) < 1e-6) ?? 2;
}

/** The confidence as shown: rounded down — at the threshold's decimals — so a missed threshold never reads as met. */
export function formatConfidence(confidence: number, decimals = 0): string {
  const scale = 10 ** decimals;
  // 0.57 * 100 is 56.99999999999999 in binary floating point
  return `${(Math.floor(confidence * 100 * scale + 1e-6) / scale).toFixed(decimals)}%`;
}

/** A threshold as shown: exact, at its own decimals ("60%", "60.5%"). */
export function formatThreshold(threshold: number): string {
  return `${(threshold * 100).toFixed(thresholdDecimals(threshold))}%`;
}

/** Decisions taken against the threshold, and the sign each one's comparison reads (SDD §14.4: decide() compared). */
const COMPARED: Partial<Record<AdvisorRecommendation['decision']['reason_code'], '<' | '≥'>> = {
  BELOW_THRESHOLD_DEFAULT_KEPT: '<',
  ABOVE_THRESHOLD_AUTO_APPLY: '≥',
  // the evidence check comes after the threshold, so its confidence met it
  INSUFFICIENT_EVIDENCE: '≥',
};

/**
 * " (57% < 60%)" for a decision taken against the threshold, else "" (the review of 10 October 2026, SDD §16).
 * The sign is the decision's, never the dashboard's own comparison of the numbers.
 */
function comparison(rec: AdvisorRecommendation): string {
  const sign = COMPARED[rec.decision.reason_code];
  const threshold = rec.decision.threshold_used;
  const confidence = rec.recommendation.confidence;
  if (!sign || threshold === null || confidence === null) return '';
  return ` (${formatConfidence(confidence, thresholdDecimals(threshold))} ${sign} ${formatThreshold(threshold)})`;
}

export interface RecommendedParts {
  /** The option's label, or why there is none. */
  option: string;
  /** "57%", or null when no confidence was given. */
  confidence: string | null;
  /** The threshold of a decision a policy took before any comparison ("60%"), or null: the others show it in their comparison. */
  threshold: string | null;
}

/** The pieces of the "Recommended: …" line; the confidence appears once (ADV-03). */
export function recommendedParts(rec: AdvisorRecommendation): RecommendedParts {
  const { option, confidence } = rec.recommendation;
  const threshold = rec.decision.threshold_used;
  if (option === null) {
    return { option: rec.model.name === 'rules' ? 'nothing — the advisor did not answer' : 'nothing — the advisor gave no option', confidence: null, threshold: null };
  }
  return {
    option: optionLabel(option),
    confidence: confidence === null ? null : formatConfidence(confidence, threshold === null ? 0 : thresholdDecimals(threshold)),
    // every entry with a threshold_used shows it once: in the Decision's comparison, or here (SDD §16)
    threshold: threshold === null || comparison(rec) ? null : formatThreshold(threshold),
  };
}

/** What the advisor recommended: "Warm migration · 57% confidence (threshold 60%)" (the line reads "Recommended: …"). */
export function recommendedText(rec: AdvisorRecommendation): string {
  const parts = recommendedParts(rec);
  if (rec.recommendation.option === null) return parts.option;
  const confidence = parts.confidence === null ? ' · no confidence given' : ` · ${parts.confidence} confidence`;
  return `${parts.option}${confidence}${parts.threshold === null ? '' : ` (threshold ${parts.threshold})`}`;
}

/** Why the decision is what it is, read from the decision alone; the numbers are on the "Recommended" line. */
function decisionReason(rec: AdvisorRecommendation): string {
  const d = rec.decision;
  switch (d.reason_code) {
    case 'ABOVE_THRESHOLD_AUTO_APPLY':
      return d.policy_id ? `applied automatically by policy ${d.policy_id}${comparison(rec)}` : `applied automatically${comparison(rec)}`;
    case 'BELOW_THRESHOLD_DEFAULT_KEPT':
      return `below threshold${comparison(rec)}, default kept`;
    case 'ADVISOR_UNAVAILABLE_FALLBACK':
      return rec.model.name === 'rules' ? 'advisor unavailable, decided by rules' : 'no usable recommendation, decided by rules';
    case 'POLICY_REQUIRES_APPROVAL':
      return d.policy_id ? `policy ${d.policy_id} requires approval` : 'the policy requires approval';
    case 'INSUFFICIENT_EVIDENCE':
      return `too little evidence to apply it automatically${comparison(rec)}`;
    case 'APPROVED_BY_USER':
      return `approved by ${d.actor.id}`;
    case 'OVERRIDDEN_BY_USER':
      return `overridden by ${d.actor.id}`;
  }
}

/** Who decides a decision that waits: the role that holds `advisor.approve`, with its article (SDD §13.2, §16). */
const DECIDER = `${/^[aeiou]/.test(PERMISSIONS['advisor.approve']) ? 'an' : 'a'} ${PERMISSIONS['advisor.approve']}`;

/** What was decided and why: "Cold migration — below threshold, default kept" (the line reads "Decision: …"). */
export function decisionText(rec: AdvisorRecommendation): string {
  const { final_option: final, status } = rec.decision;
  // no final option: a decision waiting for approval - by whom, also for those who see no controls - or a legacy note
  // that did not record the default
  const shownFinal = final !== null ? optionLabel(final) : status === 'pending_approval' ? `Waiting for approval by ${DECIDER}` : 'Not recorded';
  return `${shownFinal} — ${decisionReason(rec)}`;
}

/** The full distribution for the Details tab, highest first. */
export function distributionRows(rec: AdvisorRecommendation): Array<{ option: string; label: string; pct: string }> {
  return Object.entries(rec.recommendation.distribution)
    .sort(([, a], [, b]) => b - a)
    .map(([option, value]) => ({ option, label: distributionLabel(option), pct: formatPct(value * 100, 0) }));
}

/** Strategy notes are listed as (legacy) recommendations (SDD §14.4), so note lists leave them out. */
export function nonStrategyNotes<T extends Pick<AdvisorNote, 'kind'>>(notes: T[]): T[] {
  return notes.filter((n) => n.kind !== 'strategy');
}

/** Phases in which a migration's strategy can still change (SDD §14.6). */
const CHANGEABLE = new Set(['pending', 'blocked', 'ready']);

/**
 * The recommendation a migration waits on (`advisor_hold`) while it can still take the decision (SDD §14.6): a
 * cancelled migration takes none, whatever a hold stored before a cancel cleared it says.
 */
export function waitingDecision(m: Pick<Migration, 'advisor_hold' | 'phase'>): string | null {
  return m.advisor_hold && CHANGEABLE.has(m.phase) ? m.advisor_hold : null;
}

/**
 * The id of the recommendation a person's decision on `migration` acts on, the API's own choice (SDD §14.6): the one its
 * hold names, else the one its last validation stored - both written with it, whatever their time after the clock stepped
 * back (reviews RV50-09, RV51-08) -, else `newestId`. Every view that names that decision asks here (PR #58 review).
 */
export function decidingRecommendationId(
  migration: Pick<Migration, 'advisor_hold' | 'last_recommendation'>,
  newestId: string | undefined,
): string | undefined {
  return migration.advisor_hold || migration.last_recommendation || newestId || undefined;
}

/** Whether a person may decide on `rec` now, for a migration that has not started (SDD §14.6). */
export function decidable(rec: AdvisorRecommendation, migration: Migration | undefined, newestId: string | undefined): migration is Migration {
  return (
    !rec.legacy &&
    rec.decision_type === 'migration_strategy' &&
    migration !== undefined &&
    CHANGEABLE.has(migration.phase) &&
    decidingRecommendationId(migration, newestId) === rec.id
  );
}
