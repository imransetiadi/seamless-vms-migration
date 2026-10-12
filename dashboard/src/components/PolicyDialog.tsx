import { CircleAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type RefObject } from 'react';
import { usePlans, useProviders, useSavePolicy } from '../api/hooks';
import {
  DECISION_TYPES,
  POLICY_MIN_EVIDENCE_MAX,
  POLICY_NAME_MAX,
  type AdvisorPolicy,
  type AdvisorPolicySpec,
  type DecisionType,
  type PolicyMode,
} from '../api/types';
import { DECISION_TYPE_LABELS } from '../lib/advisor';
import { Button } from './Button';
import { Checkbox } from './Checkbox';
import { ErrorBanner } from './ErrorBanner';
import { SelectField, TextField } from './Field';
import { Modal } from './Modal';

type ScopeKind = 'everywhere' | 'providers' | 'plans';

interface FormState {
  name: string;
  scope: ScopeKind;
  providerIds: string[];
  planIds: string[];
  decisionTypes: DecisionType[];
  mode: PolicyMode;
  /** The threshold as a percent, 0..100. */
  threshold: string;
  minEvidence: string;
  requireReason: boolean;
  requireDistinctApprover: boolean;
}

type FieldKey = 'name' | 'scope' | 'threshold' | 'minEvidence';
type Errors = Partial<Record<FieldKey, string>>;

const plain = (n: number): string => String(Number(n.toFixed(2)));

function formOf(policy: AdvisorPolicy | undefined, defaultThreshold: number): FormState {
  if (!policy) {
    // a new policy asks for approval: the safe choice until its author decides otherwise
    return {
      name: '',
      scope: 'everywhere',
      providerIds: [],
      planIds: [],
      decisionTypes: [],
      mode: 'always_require_approval',
      threshold: plain(defaultThreshold * 100),
      // a new policy asks for 3 facts, like the API (SDD §4.2, ADV-02)
      minEvidence: '3',
      requireReason: true,
      requireDistinctApprover: true,
    };
  }
  const { provider_ids, plan_ids } = policy.scope;
  return {
    name: policy.name,
    scope: plan_ids.length ? 'plans' : provider_ids.length ? 'providers' : 'everywhere',
    providerIds: [...provider_ids],
    planIds: [...plan_ids],
    decisionTypes: [...policy.decision_types],
    mode: policy.mode,
    threshold: plain(policy.threshold * 100),
    minEvidence: String(policy.min_evidence_for_auto_apply),
    requireReason: policy.require_override_reason,
    requireDistinctApprover: policy.require_distinct_approver,
  };
}

function validate(f: FormState): { errors: Errors; spec: AdvisorPolicySpec | null } {
  const e: Errors = {};
  const name = f.name.trim();
  if (!name) e.name = 'Enter a policy name.';
  if (f.scope === 'providers' && f.providerIds.length === 0) e.scope = 'Choose at least one source provider.';
  if (f.scope === 'plans' && f.planIds.length === 0) e.scope = 'Choose at least one plan.';
  const threshold = f.threshold.trim() === '' ? Number.NaN : Number(f.threshold);
  if (!(threshold >= 0 && threshold <= 100)) e.threshold = 'Enter the threshold as a percent from 0 to 100.';
  const minEvidence = f.minEvidence.trim() === '' ? Number.NaN : Number(f.minEvidence);
  if (!Number.isInteger(minEvidence) || minEvidence < 0 || minEvidence > POLICY_MIN_EVIDENCE_MAX) {
    e.minEvidence = `Enter a whole number from 0 to ${POLICY_MIN_EVIDENCE_MAX}.`;
  }
  if (Object.keys(e).length > 0) return { errors: e, spec: null };
  return {
    errors: e,
    spec: {
      name,
      scope: { provider_ids: f.scope === 'providers' ? f.providerIds : [], plan_ids: f.scope === 'plans' ? f.planIds : [] },
      decision_types: DECISION_TYPES.filter((t) => f.decisionTypes.includes(t)),
      mode: f.mode,
      threshold: Number((threshold / 100).toFixed(4)),
      min_evidence_for_auto_apply: minEvidence,
      require_override_reason: f.requireReason,
      require_distinct_approver: f.requireDistinctApprover,
    },
  };
}

/** Checked ids, keeping one the list no longer has so it can be unchecked (the API refuses an unknown id). */
function choices(known: Array<{ id: string; name: string }>, chosen: string[]): Array<{ id: string; label: string }> {
  const names = new Map(known.map((k) => [k.id, k.name]));
  const gone = chosen.filter((id) => !names.has(id)).map((id) => ({ id, label: `${id} (not found)` }));
  return [...known.map((k) => ({ id: k.id, label: k.name })), ...gone];
}

const toggled = <T,>(list: T[], item: T, on: boolean): T[] => (on ? [...list, item] : list.filter((x) => x !== item));

export interface PolicyDialogProps {
  open: boolean;
  /** Edit this policy (PUT) instead of creating one (POST). */
  policy?: AdvisorPolicy;
  /** The built-in default's threshold, 0..1: a new policy starts from it. */
  defaultThreshold: number;
  /** Where focus goes on close when the control that opened the dialog may be gone (SDD §16). */
  returnFocusRef?: RefObject<HTMLElement>;
  onClose: () => void;
  onSaved: (policy: AdvisorPolicy, message: string) => void;
}

/** Create or replace an advisor policy (`advisor.policy.manage`, SDD §12, §14.6). */
export function PolicyDialog({ open, policy, defaultThreshold, returnFocusRef, onClose, onSaved }: PolicyDialogProps) {
  const titleId = useId();
  const id = (key: string) => `policy-${key}`;
  const save = useSavePolicy();
  const providers = useProviders();
  const plans = usePlans();
  const nameRef = useRef<HTMLInputElement>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const [form, setForm] = useState<FormState>(() => formOf(policy, defaultThreshold));
  const [errors, setErrors] = useState<Errors>({});

  useEffect(() => {
    if (!open) return;
    setForm(formOf(policy, defaultThreshold));
    setErrors({});
    save.reset();
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));
  const sources = (providers.data ?? []).filter((p) => p.role === 'source');
  const scopeItems =
    form.scope === 'providers' ? choices(sources, form.providerIds) : form.scope === 'plans' ? choices(plans.data ?? [], form.planIds) : [];
  const scopeQuery = form.scope === 'providers' ? providers : plans;
  const errorEntries = Object.entries(errors) as Array<[FieldKey, string]>;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const { errors: found, spec } = validate(form);
    setErrors(found);
    if (!spec) {
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    save.mutate(
      { id: policy?.id, spec },
      {
        onSuccess: (saved) => {
          onSaved(saved, policy ? `Policy ${saved.name} saved.` : `Policy ${saved.name} created.`);
          onClose();
        },
      },
    );
  };

  return (
    <Modal open={open} onClose={onClose} labelledBy={titleId} dismissible={!save.isPending} initialFocusRef={nameRef} returnFocusRef={returnFocusRef}>
      <form onSubmit={submit} noValidate className="flex min-h-0 flex-col">
        <div className="border-b border-border px-5 py-4">
          <h2 id={titleId} className="text-lg font-semibold text-foreground">
            {policy ? 'Edit policy' : 'New policy'}
          </h2>
          <p className="text-sm text-muted-foreground">Applies from the next validation. Decisions already taken stay as they are.</p>
        </div>

        {/* the body scrolls between the title and the actions: they never cover the focused control (SDD §16) */}
        <div className="relative flex min-h-0 flex-col gap-5 overflow-y-auto px-5 py-4">
          {errorEntries.length > 0 && (
            <div ref={summaryRef} tabIndex={-1} role="alert" aria-labelledby={id('summary')} className="rounded-md border border-status-danger/40 bg-status-danger/10 p-3 outline-hidden">
              <p id={id('summary')} className="flex items-center gap-2 font-medium text-foreground">
                <CircleAlert aria-hidden className="size-4 text-status-danger" />
                Fix {errorEntries.length} problem{errorEntries.length === 1 ? '' : 's'} to save the policy
              </p>
              <ul className="mt-1 list-disc pl-6 text-sm">
                {errorEntries.map(([key, message]) => (
                  <li key={key}>
                    <a
                      href={`#${id(key)}`}
                      className="inline-block py-1 text-foreground underline underline-offset-4"
                      onClick={(e) => {
                        e.preventDefault();
                        document.getElementById(id(key))?.focus();
                      }}
                    >
                      {message}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <TextField
            ref={nameRef}
            id={id('name')}
            label="Name"
            required
            maxLength={POLICY_NAME_MAX}
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            error={errors.name}
            autoComplete="off"
          />

          <div className="flex flex-col gap-2">
            <SelectField
              id={id('scope-kind')}
              label="Applies to"
              value={form.scope}
              onChange={(e) => set('scope', e.target.value as ScopeKind)}
              options={[
                { value: 'everywhere', label: 'Every plan' },
                { value: 'providers', label: 'Plans from these source providers' },
                { value: 'plans', label: 'These plans' },
              ]}
              hint="A plan's own policy wins over its source provider's, which wins over one for every plan."
            />
            {form.scope !== 'everywhere' && (
              <fieldset
                id={id('scope')}
                tabIndex={-1}
                aria-describedby={errors.scope ? id('scope-error') : undefined}
                className="flex min-w-0 flex-col rounded-md border border-border px-3 py-2 outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
              >
                <legend className="px-1 text-xs text-muted-foreground">{form.scope === 'providers' ? 'Source providers' : 'Plans'}</legend>
                {scopeQuery.error && !scopeQuery.data ? (
                  <ErrorBanner error={scopeQuery.error} title={form.scope === 'providers' ? 'Providers are unavailable' : 'Plans are unavailable'} onRetry={() => void scopeQuery.refetch()} />
                ) : scopeItems.length === 0 ? (
                  <p className="py-2 text-sm text-muted-foreground">{scopeQuery.isPending ? 'Loading…' : form.scope === 'providers' ? 'No source provider is connected.' : 'No plan exists yet.'}</p>
                ) : (
                  <div className="max-h-48 overflow-y-auto">
                    {scopeItems.map((item) => {
                      const key = form.scope === 'providers' ? 'providerIds' : 'planIds';
                      return (
                        <Checkbox key={item.id} label={item.label} checked={form[key].includes(item.id)} onChange={(on) => set(key, toggled(form[key], item.id, on))} />
                      );
                    })}
                  </div>
                )}
                {errors.scope && (
                  <p id={id('scope-error')} className="field-error">
                    <CircleAlert aria-hidden className="size-3.5 shrink-0" />
                    {errors.scope}
                  </p>
                )}
              </fieldset>
            )}
          </div>

          <fieldset className="flex min-w-0 flex-col">
            <legend className="field-label">Decisions</legend>
            <p id={id('types-hint')} className="field-hint mt-0">
              Leave every box unchecked to cover every kind of decision, including ones added later.
            </p>
            <div className="grid sm:grid-cols-2">
              {DECISION_TYPES.map((type) => (
                <Checkbox
                  key={type}
                  label={DECISION_TYPE_LABELS[type]}
                  checked={form.decisionTypes.includes(type)}
                  onChange={(on) => set('decisionTypes', toggled(form.decisionTypes, type, on))}
                  describedBy={id('types-hint')}
                />
              ))}
            </div>
          </fieldset>

          <SelectField
            id={id('mode')}
            label="When the advisor decides"
            value={form.mode}
            onChange={(e) => set('mode', e.target.value as PolicyMode)}
            options={[
              { value: 'always_require_approval', label: 'Ask a person to approve every decision' },
              { value: 'auto_apply_above_threshold', label: 'Apply it when the confidence reaches the threshold' },
            ]}
          />

          <div className="grid gap-3 sm:grid-cols-2">
            <TextField
              id={id('threshold')}
              label="Threshold (%)"
              type="number"
              inputMode="decimal"
              min={0}
              max={100}
              step="any"
              value={form.threshold}
              onChange={(e) => set('threshold', e.target.value)}
              error={errors.threshold}
              hint="Below it, the default strategy is kept."
            />
            <TextField
              id={id('minEvidence')}
              label="Minimum evidence"
              type="number"
              inputMode="numeric"
              min={0}
              max={POLICY_MIN_EVIDENCE_MAX}
              step={1}
              value={form.minEvidence}
              onChange={(e) => set('minEvidence', e.target.value)}
              error={errors.minEvidence}
              hint="Fewer facts that support or oppose the decision send it for approval; each recommendation's evidence card lists them."
            />
          </div>

          <Checkbox
            label="Require a reason to override"
            hint="At least 10 characters, saved in the audit trail."
            checked={form.requireReason}
            onChange={(on) => set('requireReason', on)}
          />
          <Checkbox
            label="Require a distinct approver"
            hint="The person whose validation asked for a decision cannot approve or override it."
            checked={form.requireDistinctApprover}
            onChange={(on) => set('requireDistinctApprover', on)}
          />

          {save.error && <ErrorBanner error={save.error} title={policy ? 'The policy was not saved' : 'The policy was not created'} />}
        </div>

        <div className="flex flex-col-reverse gap-2 border-t border-border bg-card px-5 py-3 sm:flex-row sm:justify-end">
          <Button size="lg" onClick={onClose} disabledReason={save.isPending ? 'Wait for the request to finish.' : null}>
            Cancel
          </Button>
          <Button size="lg" type="submit" variant="primary" loading={save.isPending}>
            {policy ? 'Save policy' : 'Create policy'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
