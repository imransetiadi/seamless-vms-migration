import { Replace, ThumbsUp } from 'lucide-react';
import { useRef, useState, type RefObject } from 'react';
import { useDecideRecommendation, useMe, usePlan, usePolicies } from '../api/hooks';
import { ACTION_TEXT_MAX, ACTION_TEXT_UNITS, OVERRIDE_REASON_MIN, type AdvisorRecommendation, type Migration, type MigrationSummary, type Strategy, type VMRef } from '../api/types';
import { optionLabel } from '../lib/advisor';
import { pyLen, pyStrip, pyTooLong } from '../lib/format';
import { resolvePolicy } from '../lib/policies';
import { OWN_DECISION, ownReason } from '../lib/roles';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { SelectField, TextAreaField } from './Field';

/** What the actions read of a migration, or of its summary: its plan, its VM and the strategies eligible now (SDD §4). */
export type ActionMigration = Pick<Migration, 'plan_id'> & { vm: Pick<VMRef, 'source_id'> } & (Pick<Migration, 'estimates'> | Pick<MigrationSummary, 'eligible_strategies'>);

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
  migration: ActionMigration;
  /** Where focus goes after a decision, when these actions may go with it (SDD §16). */
  focusAfterRef?: RefObject<HTMLElement | null>;
  /** Says what was decided, for the page's live region. */
  onDecided?: (message: string) => void;
}) {
  const decide = useDecideRecommendation(rec.id);
  const policies = usePolicies();
  const [open, setOpen] = useState<'approve' | 'override' | null>(null);
  // a summary names them as the API reads its estimates (SDD §4)
  const eligible = 'eligible_strategies' in migration ? migration.eligible_strategies : migration.estimates.filter((e) => e.eligible).map((e) => e.strategy);
  const recommended = rec.recommendation.option;
  const canApprove = recommended !== null && eligible.includes(recommended as Strategy);
  // another eligible option: running the recommended one is approving it (SDD §16) - unless Approve cannot run it, an
  // answer that could not be applied, which stays a person's call (SDD §14.6, audit AD-03, review RV50-05)
  const alternatives = eligible.filter((s) => s !== recommended || rec.recommendation.unusable);
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
  // stripped and counted as the API's Python does: code points, an emoji one (SDD §14.6, audit AD-10)
  const reasonOk = !reasonRequired || pyLen(pyStrip(reason)) >= OVERRIDE_REASON_MIN;
  // and capped as it counts them: the field's maxLength counts UTF-16 units (review RV50-06)
  const reasonTooLong = pyTooLong(reason, ACTION_TEXT_MAX);
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
  // its default strategy too: a validation honours it as an override (audit AD-04)
  const plan = usePlan(migration.plan_id).data;
  const pinned = plan?.strategy_overrides[migration.vm.source_id] ?? (plan && plan.default_strategy !== 'auto' ? plan.default_strategy : undefined);
  const pinnedReason = pinned ? `The plan sets the strategy of ${name} (${optionLabel(pinned)}): change it in the plan.` : null;
  // an answer that could not be applied stays unapplied; an override is a person's call (audit AD-03)
  const unusableReason = rec.recommendation.unusable ? `nothing to approve: ${rec.recommendation.unusable}` : null;
  const approveReason = own
    ? ownReason(OWN_DECISION, me)
    : (pinnedReason ?? (rec.decision.status === 'approved' ? 'The decision is already approved.' : (unusableReason ?? (canApprove ? null : 'The recommended option is not eligible for this VM now.'))));
  const overrideReason = own ? ownReason(OWN_DECISION, me) : (pinnedReason ?? (alternatives.length ? null : 'No other option is eligible for this VM.'));
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
        canConfirm={option !== '' && reasonOk && !reasonTooLong}
        confirmReason={option === '' ? 'Choose an option first.' : (reasonTooLong ?? `Enter a reason of at least ${OVERRIDE_REASON_MIN} characters.`)}
        pending={decide.isPending}
        error={decide.error}
        returnFocusRef={returnRef}
        onConfirm={() =>
          decide.mutate(
            // the reason counted is the reason sent (review RV50-06)
            { action: 'override', body: { option: option as Strategy, reason: pyStrip(reason) || null } },
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
          maxLength={ACTION_TEXT_UNITS}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          hint={reasonRequired ? `The policy asks for one: at least ${OVERRIDE_REASON_MIN} characters, recorded in the audit trail.` : 'Recorded in the audit trail.'}
        />
      </ConfirmDialog>
    </>
  );
}
