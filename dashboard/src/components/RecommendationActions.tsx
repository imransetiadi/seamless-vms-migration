import { Replace, ThumbsUp } from 'lucide-react';
import { useRef, useState, type RefObject } from 'react';
import { useDecideRecommendation, useMe, usePlan, usePolicies } from '../api/hooks';
import { ACTION_TEXT_MAX, OVERRIDE_REASON_MIN, type AdvisorRecommendation, type Migration, type Strategy } from '../api/types';
import { optionLabel } from '../lib/advisor';
import { resolvePolicy } from '../lib/policies';
import { OWN_DECISION } from '../lib/roles';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { SelectField, TextAreaField } from './Field';

/**
 * Approve the advisor's option or override it with another eligible one (SDD §14.6). Callers render it
 * only for `advisor.approve`: without the permission there is nothing to show.
 */
export function RecommendationActions({
  rec,
  migration,
  focusAfterRef,
  onDecided,
}: {
  rec: AdvisorRecommendation;
  migration: Migration;
  /** Where focus goes after a decision, when these actions may go with it (SDD §16). */
  focusAfterRef?: RefObject<HTMLElement>;
  /** Says what was decided, for the page's live region. */
  onDecided?: (message: string) => void;
}) {
  const decide = useDecideRecommendation(rec.id);
  const policies = usePolicies();
  const [open, setOpen] = useState<'approve' | 'override' | null>(null);
  const eligible = migration.estimates.filter((e) => e.eligible).map((e) => e.strategy);
  const recommended = rec.recommendation.option;
  const canApprove = recommended !== null && eligible.includes(recommended as Strategy);
  // another eligible option: running the recommended one is approving it (SDD §16)
  const alternatives = eligible.filter((s) => s !== recommended);
  const [option, setOption] = useState<Strategy | ''>(() => alternatives[0] ?? '');
  const [reason, setReason] = useState('');
  // the policy in force says whether a reason is required when the decision recorded no rule, as the API checks it;
  // unknown: ask for one (SDD §14.6)
  const inForce = policies.data ? (resolvePolicy(policies.data.items, rec.plan_id, rec.provider_id, rec.decision_type) ?? policies.data.default) : undefined;
  // the rules the decision recorded win: a policy changed since does not rewrite it (SDD §14.6)
  const reasonRequired = rec.decision.require_override_reason ?? (inForce ? inForce.require_override_reason : true);
  // maker-checker (SDD §14.6): the user whose validation asked for this decision does not decide it
  const me = useMe().data?.name;
  const own = Boolean(me && rec.requested_by === me && (rec.decision.require_distinct_approver ?? inForce?.require_distinct_approver ?? true));
  const reasonOk = !reasonRequired || reason.trim().length >= OVERRIDE_REASON_MIN;
  // read by the dialog as it closes: empty on Cancel (focus goes back to the button), set after a decision
  const returnRef = useRef<HTMLElement | null>(null);
  const close = () => {
    returnRef.current = null;
    setOpen(null);
    decide.reset();
  };
  const decided = (message: string) => {
    returnRef.current = focusAfterRef?.current ?? null;
    setOpen(null);
    decide.reset();
    onDecided?.(message);
  };
  const name = rec.subject.name;
  // like the API (SDD §14.6), after the requester: the plan's own choice of strategy stands over the newest
  // recommendation, and an approved decision is not approved again
  const pinned = usePlan(migration.plan_id).data?.strategy_overrides[migration.vm.source_id];
  const pinnedReason = pinned ? `The plan sets the strategy of ${name} (${optionLabel(pinned)}): change it in the plan.` : null;
  const approveReason = own
    ? OWN_DECISION
    : (pinnedReason ?? (rec.decision.status === 'approved' ? 'The decision is already approved.' : canApprove ? null : 'The recommended option is not eligible for this VM now.'));
  const overrideReason = own ? OWN_DECISION : (pinnedReason ?? (alternatives.length ? null : 'No other option is eligible for this VM.'));
  return (
    <>
      <div role="group" aria-label={`Decide on the recommendation for ${name}`} className="mt-2 flex flex-wrap gap-2">
        <Button size="lg" variant="primary" icon={ThumbsUp} disabledReason={approveReason} onClick={() => setOpen('approve')}>
          Approve
        </Button>
        <Button size="lg" icon={Replace} disabledReason={overrideReason} onClick={() => setOpen('override')}>
          Override
        </Button>
      </div>
      <ConfirmDialog
        open={open === 'approve'}
        title={`Approve ${recommended ? optionLabel(recommended) : 'the recommendation'} for ${name}?`}
        description="The migration then runs this option. Your approval is recorded in the audit trail."
        confirmLabel="Approve"
        pending={decide.isPending}
        error={decide.error}
        returnFocusRef={returnRef}
        onConfirm={() => decide.mutate({ action: 'approve' }, { onSuccess: () => decided(`Approved: ${name} runs ${optionLabel(recommended ?? '')}.`) })}
        onCancel={close}
      />
      <ConfirmDialog
        open={open === 'override'}
        title={`Override the recommendation for ${name}?`}
        description="Choose the option the migration runs instead. The plan keeps your choice, and the audit trail records it."
        confirmLabel="Override"
        canConfirm={option !== '' && reasonOk}
        confirmReason={option === '' ? 'Choose an option first.' : `Enter a reason of at least ${OVERRIDE_REASON_MIN} characters.`}
        pending={decide.isPending}
        error={decide.error}
        returnFocusRef={returnRef}
        onConfirm={() =>
          decide.mutate(
            { action: 'override', body: { option: option as Strategy, reason: reason.trim() || null } },
            { onSuccess: () => decided(`Overridden: ${name} runs ${optionLabel(option)}.`) },
          )
        }
        onCancel={close}
      >
        <SelectField label="Run instead" value={option} onChange={(e) => setOption(e.target.value as Strategy)} options={alternatives.map((s) => ({ value: s, label: optionLabel(s) }))} />
        <TextAreaField
          label={reasonRequired ? 'Reason' : 'Reason (optional)'}
          required={reasonRequired}
          rows={2}
          maxLength={ACTION_TEXT_MAX}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          hint={reasonRequired ? `The policy asks for one: at least ${OVERRIDE_REASON_MIN} characters, recorded in the audit trail.` : 'Recorded in the audit trail.'}
        />
      </ConfirmDialog>
    </>
  );
}
