import { Hourglass, Pencil, Plus, ShieldCheck, SlidersHorizontal, Trash2, Zap } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { usePlans, usePolicies, useProviders, useDeletePolicy } from '../api/hooks';
import { useRole } from '../api/session';
import type { AdvisorPolicy } from '../api/types';
import { Button } from '../components/Button';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { EmptyState } from '../components/EmptyState';
import { ErrorBanner } from '../components/ErrorBanner';
import { PageHeader } from '../components/PageHeader';
import { PolicyDialog } from '../components/PolicyDialog';
import { LoadingBlock } from '../components/Skeleton';
import { StatusBadge } from '../components/StatusBadge';
import { formatThreshold } from '../lib/advisor';
import { formatDateTime, formatRelative } from '../lib/format';
import { distinctApproverText, overrideReasonText, POLICY_MODE_LABELS, policyDecisionsText, policyScopeText } from '../lib/policies';
import { providerLabel } from '../lib/providerUse';
import { can } from '../lib/roles';
import { useHashLanding } from '../lib/useHashLanding';
import { usePageTitle } from '../lib/usePageTitle';

const MANAGE_REASON = 'Managing advisor policies requires the admin role.';

/** The resolution level of a policy: a plan's wins over a provider's over a global one (SDD §14.6). */
function level(policy: AdvisorPolicy): number {
  return policy.scope.plan_ids.length ? 0 : policy.scope.provider_ids.length ? 1 : 2;
}

function ModeBadge({ policy }: { policy: AdvisorPolicy }) {
  return policy.mode === 'always_require_approval' ? (
    <StatusBadge tone="warning" icon={Hourglass} label={POLICY_MODE_LABELS[policy.mode]} />
  ) : (
    <StatusBadge tone="success" icon={Zap} label={POLICY_MODE_LABELS[policy.mode]} />
  );
}

function PolicyCard({
  policy,
  names,
  builtIn = false,
  canManage,
  onEdit,
  onDelete,
}: {
  policy: AdvisorPolicy;
  names: Map<string, string>;
  builtIn?: boolean;
  canManage: boolean;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const headingId = useId();
  // worded as the audit trail lists a policy change (SDD §14.5)
  const scope = builtIn ? 'Plans no other policy covers' : policyScopeText(policy.scope, names);
  return (
    <li>
      {/* addressable as #policy-{id}: the plan settings link to the policy in force (SDD §16, ADV-20) */}
      <article
        id={`policy-${policy.id}`}
        tabIndex={-1}
        aria-labelledby={headingId}
        className="card flex h-full min-w-0 scroll-mt-20 flex-col gap-3 p-4 outline-offset-2 focus:outline-2 focus:outline-ring"
      >
        <header className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1 basis-full sm:basis-auto">
            <h3 id={headingId} className="text-base font-semibold text-foreground wrap-break-word">
              {builtIn ? 'Built-in default' : policy.name}
            </h3>
            <p className="text-xs text-muted-foreground">
              <code>{policy.id}</code>
            </p>
          </div>
          <ModeBadge policy={policy} />
        </header>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Applies to</dt>
          <dd className="min-w-0 wrap-break-word">{scope}</dd>
          <dt className="text-muted-foreground">Decisions</dt>
          <dd className="min-w-0">{policyDecisionsText(policy.decision_types)}</dd>
          <dt className="text-muted-foreground">Threshold</dt>
          <dd className="num">{formatThreshold(policy.threshold)}</dd>
          <dt className="text-muted-foreground">Minimum evidence</dt>
          <dd className="num">{policy.min_evidence_for_auto_apply}</dd>
          <dt className="text-muted-foreground">Override reason</dt>
          <dd>{overrideReasonText(policy.require_override_reason)}</dd>
          <dt className="text-muted-foreground">Distinct approver</dt>
          <dd>{distinctApproverText(policy.require_distinct_approver)}</dd>
          {!builtIn && (
            <>
              <dt className="text-muted-foreground">Changed</dt>
              <dd>
                <time dateTime={policy.updated_at} title={formatDateTime(policy.updated_at)}>
                  {formatRelative(policy.updated_at)}
                </time>{' '}
                by {policy.updated_by}
              </dd>
            </>
          )}
        </dl>
        <footer className="mt-auto flex flex-wrap items-center gap-2 pt-1">
          {builtIn ? (
            <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <ShieldCheck aria-hidden className="size-3.5" />
              Read-only. Its threshold is the server setting SEAMLESS_JEV_MIN_CONFIDENCE.
            </p>
          ) : (
            <>
              <Button icon={Pencil} onClick={onEdit} disabledReason={canManage ? null : MANAGE_REASON}>
                Edit<span className="sr-only"> {policy.name}</span>
              </Button>
              <Button variant="ghost" icon={Trash2} className="ml-auto" onClick={onDelete} disabledReason={canManage ? null : MANAGE_REASON}>
                Delete<span className="sr-only"> {policy.name}</span>
              </Button>
            </>
          )}
        </footer>
      </article>
    </li>
  );
}

/** The advisor's policies: when its decision applies on its own and when a person approves it (SDD §14.6, §16). */
export default function AdvisorPolicies() {
  usePageTitle('Advisor policies');
  const policies = usePolicies();
  const providers = useProviders();
  const plans = usePlans();
  const remove = useDeletePolicy();
  const canManage = can(useRole(), 'advisor.policy.manage');
  const [dialog, setDialog] = useState<{ open: boolean; policy?: AdvisorPolicy }>({ open: false });
  const [deleting, setDeleting] = useState<AdvisorPolicy | null>(null);
  const [deleted, setDeleted] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const newRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const headingId = useId();

  const names = useMemo(
    // a provider by its label - its name, else its id (SDD §4.2, review RV22-01) -, a plan by its name
    () =>
      new Map([
        ...(providers.data ?? []).map((p) => [p.id, providerLabel(p)] as [string, string]),
        ...(plans.data ?? []).map((p) => [p.id, p.name] as [string, string]),
      ]),
    [providers.data, plans.data],
  );
  const items = useMemo(
    () => [...(policies.data?.items ?? [])].sort((a, b) => level(a) - level(b) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id)),
    [policies.data],
  );
  useEffect(() => {
    // the deleted card went with the button the dialog returns focus to: the list heading takes it (SDD §16)
    if (deleted && !items.some((p) => p.id === deleted)) {
      headingRef.current?.focus();
      setDeleted(null);
    }
  }, [deleted, items]);

  // a link to #policy-{id} lands on that policy once the list is there (SDD §16, ADV-20)
  useHashLanding(Boolean(policies.data));

  const openCreate = () => setDialog({ open: true });
  const fallback = policies.data?.default;

  return (
    <>
      <PageHeader
        breadcrumbs={[{ label: 'Advisor', to: '/advisor' }, { label: 'Policies' }]}
        title="Advisor policies"
        description="When the advisor's decision applies on its own and when a person approves it first. A plan's own policy wins, then its source provider's, then one for every plan; among equals, the strictest."
        actions={
          <Button ref={newRef} size="lg" variant="primary" icon={Plus} onClick={openCreate} disabledReason={canManage ? null : MANAGE_REASON}>
            New policy
          </Button>
        }
      />
      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>
      {policies.isPending && <LoadingBlock label="Loading policies…" rows={3} />}
      {policies.error && <ErrorBanner error={policies.error} title="Advisor policies are unavailable" onRetry={() => void policies.refetch()} />}
      {policies.data && (
        <section aria-labelledby={headingId} className="flex flex-col gap-3">
          <h2 ref={headingRef} id={headingId} tabIndex={-1} className="text-base font-semibold text-foreground outline-hidden">
            {items.length} {items.length === 1 ? 'policy' : 'policies'}
          </h2>
          {items.length === 0 && (
            <EmptyState
              icon={SlidersHorizontal}
              title="No policies yet"
              description={`The built-in default decides everywhere: the advisor's decision applies on its own from ${formatThreshold(policies.data.default.threshold)} confidence. Add a policy to have a person approve decisions first.`}
            />
          )}
          <ul className="grid gap-4 lg:grid-cols-2">
            {items.map((policy) => (
              <PolicyCard
                key={policy.id}
                policy={policy}
                names={names}
                canManage={canManage}
                onEdit={() => setDialog({ open: true, policy })}
                onDelete={() => setDeleting(policy)}
              />
            ))}
            <PolicyCard policy={policies.data.default} names={names} builtIn canManage={false} />
          </ul>
        </section>
      )}

      <PolicyDialog
        open={dialog.open}
        policy={dialog.policy}
        defaultThreshold={fallback?.threshold ?? 0.6}
        returnFocusRef={dialog.policy ? undefined : newRef}
        onClose={() => setDialog({ open: false })}
        onSaved={(_, message) => setAnnouncement(message)}
      />
      <ConfirmDialog
        open={deleting !== null}
        tone="danger"
        icon={Trash2}
        title={`Delete ${deleting?.name ?? 'the policy'}?`}
        subject={deleting ? `Policy ${deleting.id}` : undefined}
        consequences={[
          { kind: 'removed', text: 'the policy' },
          { kind: 'kept', text: 'the decisions already taken, as they are' },
          {
            kind: 'then',
            text: 'validations use the next most specific policy, or the built-in default; a migration waiting for approval keeps waiting until someone approves or overrides it, or its plan is validated again',
          },
        ]}
        confirmLabel="Delete policy"
        pending={remove.isPending}
        error={remove.error}
        onConfirm={() => {
          const target = deleting;
          if (!target) return;
          remove.mutate(target.id, {
            onSuccess: () => {
              setDeleting(null);
              setDeleted(target.id);
              setAnnouncement(`Policy ${target.name} deleted.`);
            },
          });
        }}
        onCancel={() => {
          setDeleting(null);
          remove.reset();
        }}
      />
    </>
  );
}
