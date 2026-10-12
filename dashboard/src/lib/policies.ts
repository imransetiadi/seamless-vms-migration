import type { AdvisorPolicy, DecisionType, PolicyMode, PolicyScope } from '../api/types';
import { DECISION_TYPE_LABELS, formatThreshold } from './advisor';

/** What a policy does with a confident decision (SDD §14.6). */
export const POLICY_MODE_LABELS: Record<PolicyMode, string> = {
  auto_apply_above_threshold: 'Applies above the threshold',
  always_require_approval: 'Always asks for approval',
};

/** Strictest first: approval before auto, then the higher threshold, minimum evidence, a required reason, a distinct approver, the lower id. */
function strictness(p: AdvisorPolicy): Array<number | string> {
  return [
    p.mode === 'always_require_approval' ? 0 : 1,
    -p.threshold,
    -p.min_evidence_for_auto_apply,
    p.require_override_reason ? 0 : 1,
    p.require_distinct_approver ? 0 : 1,
    p.id,
  ];
}

function stricter(a: AdvisorPolicy, b: AdvisorPolicy): number {
  const [x, y] = [strictness(a), strictness(b)];
  for (let i = 0; i < x.length; i += 1) if (x[i] !== y[i]) return x[i]! < y[i]! ? -1 : 1;
  return 0;
}

/**
 * resolve_policy (SDD §14.6): of the policies for `decisionType`, the plan's win, then its source provider's,
 * then those for every plan; the strictest within a level. `null`: none applies, the built-in default does.
 */
export function resolvePolicy(
  policies: readonly AdvisorPolicy[],
  planId: string | null,
  providerId: string | null,
  decisionType: DecisionType,
): AdvisorPolicy | null {
  const applicable = policies.filter((p) => !p.decision_types.length || p.decision_types.includes(decisionType));
  const levels = [
    applicable.filter((p) => planId !== null && p.scope.plan_ids.includes(planId)),
    applicable.filter((p) => providerId !== null && p.scope.provider_ids.includes(providerId)),
    applicable.filter((p) => !p.scope.plan_ids.length && !p.scope.provider_ids.length),
  ];
  const level = levels.find((l) => l.length);
  return level ? [...level].sort(stricter)[0]! : null;
}

/** Where a stored policy applies, as its card reads it: plans and providers by name, by id when unknown (SDD §16). */
export function policyScopeText({ provider_ids, plan_ids }: PolicyScope, names: ReadonlyMap<string, string>): string {
  const listed = (ids: readonly string[]) => ids.map((id) => names.get(id) ?? id).join(', ');
  if (plan_ids.length) return `${plan_ids.length === 1 ? 'Plan' : 'Plans'} ${listed(plan_ids)}`;
  return provider_ids.length ? `Plans from ${listed(provider_ids)}` : 'Every plan';
}

/** The decisions a policy covers: none listed is every kind (SDD §14.6). */
export function policyDecisionsText(types: readonly DecisionType[]): string {
  const labels: Record<string, string | undefined> = DECISION_TYPE_LABELS;
  return types.length ? types.map((t) => labels[t] ?? t).join(', ') : 'Every kind';
}

export const overrideReasonText = (required: boolean): string => (required ? 'Required' : 'Optional');
export const distinctApproverText = (required: boolean): string => (required ? 'Required' : 'Not required');

/** One setting a policy change changed, as the audit page lists it (SDD §14.5). */
export interface PolicyChange {
  setting: string;
  before: string;
  after: string;
}

/** A setting as compared (`key`) and as shown (`text`); null when the policy lacks it. */
type Reading = { key: string; text: string } | null;

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const strings = (value: unknown): string[] | null => (Array.isArray(value) && value.every((x) => typeof x === 'string') ? value : null);
/** A list whose order alone changed did not change. */
const unordered = (items: readonly string[]): string => JSON.stringify([...items].sort());
const flag = (value: unknown, text: (on: boolean) => string): Reading => (typeof value === 'boolean' ? { key: String(value), text: text(value) } : null);

/** The settings in the order the policies page shows them, each read from the policy an audit event holds. */
const SETTINGS: ReadonlyArray<[string, (p: Record<string, unknown>, names: ReadonlyMap<string, string>) => Reading]> = [
  ['Name', (p) => (typeof p.name === 'string' ? { key: p.name, text: p.name } : null)],
  [
    'When the advisor decides',
    (p) => {
      const labels: Record<string, string | undefined> = POLICY_MODE_LABELS;
      return typeof p.mode === 'string' ? { key: p.mode, text: labels[p.mode] ?? p.mode } : null;
    },
  ],
  [
    'Applies to',
    (p, names) => {
      const scope = isRecord(p.scope) ? p.scope : {};
      const [providers, plans] = [strings(scope.provider_ids), strings(scope.plan_ids)];
      return providers && plans ? { key: unordered(providers) + unordered(plans), text: policyScopeText({ provider_ids: providers, plan_ids: plans }, names) } : null;
    },
  ],
  [
    'Decisions',
    (p) => {
      const types = strings(p.decision_types);
      return types ? { key: unordered(types), text: policyDecisionsText(types as DecisionType[]) } : null;
    },
  ],
  ['Threshold', (p) => (typeof p.threshold === 'number' ? { key: String(p.threshold), text: formatThreshold(p.threshold) } : null)],
  ['Minimum evidence', (p) => (typeof p.min_evidence_for_auto_apply === 'number' ? { key: String(p.min_evidence_for_auto_apply), text: String(p.min_evidence_for_auto_apply) } : null)],
  ['Override reason', (p) => flag(p.require_override_reason, overrideReasonText)],
  ['Distinct approver', (p) => flag(p.require_distinct_approver, distinctApproverText)],
];

/**
 * The settings a `policy.updated` changed, from the policies it holds before and after (SDD §14.5): [] when none
 * did, null when a side is not a policy. A setting either side lacks is not compared.
 */
export function policyChanges(before: unknown, after: unknown, names: ReadonlyMap<string, string>): PolicyChange[] | null {
  if (!isRecord(before) || !isRecord(after)) return null;
  const changes: PolicyChange[] = [];
  for (const [setting, read] of SETTINGS) {
    const [was, now] = [read(before, names), read(after, names)];
    if (was && now && was.key !== now.key) changes.push({ setting, before: was.text, after: now.text });
  }
  return changes;
}
