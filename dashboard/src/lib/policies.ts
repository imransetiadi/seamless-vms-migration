import type { AdvisorPolicy, DecisionType, PolicyMode } from '../api/types';

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
