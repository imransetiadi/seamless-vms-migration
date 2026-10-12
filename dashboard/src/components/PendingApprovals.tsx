import { Bot, ShieldCheck } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { Migration, Plan } from '../api/types';
import { APPROVALS_SHOWN, approvalKindLabel, pendingApprovals, type ApprovalKind } from '../lib/approvals';
import { Button } from './Button';
import { EmptyState } from './EmptyState';
import { Panel } from './Panel';
import { Segmented } from './Segmented';
import { Unavailable } from './Unavailable';

type Filter = 'all' | ApprovalKind;

const KIND_ICONS = { cutover: ShieldCheck, advisor_decision: Bot } as const;

export interface PendingApprovalsProps {
  /** Null when the migrations could not be loaded: every pending approval is unknown. */
  migrations: Migration[] | null;
  plans: ReadonlyMap<string, Plan>;
  plansUnknown: boolean;
}

/**
 * "Pending approvals" (SDD §16, ADV-20): both kinds of approval a person still has to give, each row with its
 * kind, the VM, its plan and what it waits for, linking to where it is decided; filtered by kind. The first
 * APPROVALS_SHOWN rows of the kind, the rest behind "Show all"; choosing another kind folds them back.
 */
export function PendingApprovals({ migrations, plans, plansUnknown }: PendingApprovalsProps) {
  const [filter, setFilter] = useState<Filter>('all');
  const [all, setAll] = useState(false);
  const panel = (body: ReactNode, actions?: ReactNode) => (
    <Panel title="Pending approvals" description="Cutover approvals and advisor decisions waiting for a person" actions={actions}>
      {body}
    </Panel>
  );
  if (migrations === null) return panel(<Unavailable what="migrations" />);
  const { items, cutoverUnknown } = pendingApprovals(migrations, plans, { plansUnknown });
  const count = (kind: ApprovalKind) => items.filter((i) => i.kind === kind).length;
  const ofKind = filter === 'all' ? items : items.filter((i) => i.kind === filter);
  const shown = all ? ofKind : ofKind.slice(0, APPROVALS_SHOWN);
  const unknownShown = cutoverUnknown && filter !== 'advisor_decision';
  return panel(
    <>
      {unknownShown && <Unavailable what="plans" subject="Cutover approvals" className={shown.length ? 'mb-2' : undefined} />}
      {shown.length === 0 ? (
        !unknownShown && <EmptyState icon={ShieldCheck} title="No approval is waiting" description="Cutover approvals and advisor decisions appear here until a person gives them." />
      ) : (
        <>
          <ul className="-my-2 divide-y divide-border">
            {shown.map((item) => {
              const Icon = KIND_ICONS[item.kind];
              return (
                <li key={`${item.kind}-${item.migration.id}`} aria-label={item.migration.vm.name} className="flex gap-3 py-2.5">
                  <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium text-muted-foreground">{approvalKindLabel(item)}</p>
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <Link to={item.href} className="font-mono text-sm font-semibold text-foreground underline-offset-4 hover:underline">
                        {item.migration.vm.name}
                      </Link>
                      {item.plan && <span className="min-w-0 wrap-break-word text-xs text-muted-foreground">{item.plan.name}</span>}
                    </div>
                    <p className="mt-0.5 wrap-break-word text-sm text-foreground">{item.waitsFor}</p>
                  </div>
                </li>
              );
            })}
          </ul>
          {ofKind.length > APPROVALS_SHOWN && (
            <Button size="sm" variant="ghost" className="mt-3" aria-expanded={all} onClick={() => setAll((v) => !v)}>
              {all ? 'Show fewer' : `Show all (${ofKind.length})`}
            </Button>
          )}
        </>
      )}
    </>,
    <Segmented<Filter>
      label="Kind"
      value={filter}
      onChange={(kind) => {
        setFilter(kind);
        setAll(false);
      }}
      options={[
        { value: 'all', label: `All (${items.length})` },
        { value: 'cutover', label: `Cutover approval (${count('cutover')})` },
        { value: 'advisor_decision', label: `Advisor decision (${count('advisor_decision')})` },
      ]}
    />,
  );
}
