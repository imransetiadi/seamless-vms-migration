import { AlarmClock, BadgeCheck, Power, TriangleAlert } from 'lucide-react';
import { useId, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import { useFinalizePlan } from '../api/hooks';
import type { Migration, Plan, PlanFinalizeResult, Wave } from '../api/types';
import { completedSince, inScope, isOverdue, queueByWave, type SkippedVm } from '../lib/finalize';
import { formatDateTime, formatRelative } from '../lib/format';
import type { Availability } from '../lib/planActions';
import { useNow } from '../lib/useNow';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { Panel } from './Panel';
import { StatusBadge } from './StatusBadge';

/** What a bulk finalize covers: one wave, or the whole plan (`wave_id` null, SDD §8.1). */
export interface FinalizeScope {
  wave: Wave | null;
}

function vms(n: number): string {
  return `${n} VM${n === 1 ? '' : 's'}`;
}

export interface FinalizeQueueProps {
  plan: Plan;
  migrations: Migration[];
  /** The plan's Finalize availability (approver, a VM awaiting finalize; SDD §8.1). */
  gate: Availability;
  /** The page header already offers Finalize all (a Completed plan): the panel does not repeat it. */
  allInHeader: boolean;
  onFinalize: (scope: FinalizeScope) => void;
}

/**
 * The finalize queue (SDD §8.1, §16, ADV-19): the plan's `completed` migrations oldest first, by wave,
 * each with its time in Completed, "Finalize overdue" past the plan's `finalize_warning_h` and
 * "Source VM running" while the source watch sees it running; Finalize wave per wave.
 */
export function FinalizeQueue({ plan, migrations, gate, allInHeader, onFinalize }: FinalizeQueueProps) {
  const now = useNow(60_000);
  const groups = queueByWave(plan, migrations);
  if (groups.length === 0) return null;
  // never two buttons that finalize the same VMs: one wave group is what Finalize all finalizes
  const showAll = !allInHeader && (groups.length > 1 || groups.some((g) => g.wave === null));
  const showWave = !(allInHeader && groups.length === 1);
  const n = groups.reduce((sum, g) => sum + g.items.length, 0);
  return (
    <Panel
      title="Awaiting finalize"
      description="Verified on RHOSO. Finalize cleans up each source VM (approver); until then a rollback can restart it."
      actions={
        showAll ? (
          <Button size="sm" icon={BadgeCheck} disabledReason={gate.reason} onClick={() => onFinalize({ wave: null })}>
            Finalize all
          </Button>
        ) : undefined
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-sm text-foreground">
          {n === 1
            ? '1 VM awaits finalize: the source VM is still there until it is finalized.'
            : `${n} VMs await finalize: their source VMs are still there until they are finalized.`}
        </p>
        {groups.map(({ wave, items }) => (
          <div key={wave?.id ?? 'none'} className="flex flex-col gap-1.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-medium text-foreground">
                {wave ? wave.name : 'Not in a wave'} <span className="num font-normal text-muted-foreground">({items.length})</span>
              </h3>
              {wave && showWave && (
                <Button size="sm" icon={BadgeCheck} disabledReason={gate.reason} aria-label={`Finalize wave ${wave.name}`} onClick={() => onFinalize({ wave })}>
                  Finalize wave
                </Button>
              )}
            </div>
            <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
              {items.map((m) => {
                const since = completedSince(m);
                return (
                  <li key={m.id} aria-label={m.vm.name} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 px-3 py-2">
                    <div className="min-w-0 text-sm">
                      <Link to={`/migrations/${m.id}`} className="font-mono font-medium text-foreground underline-offset-4 hover:underline">
                        {m.vm.name}
                      </Link>
                      <p className="text-xs text-muted-foreground">
                        {since ? (
                          <>
                            Completed <time dateTime={since}>{formatRelative(since, now)}</time>
                          </>
                        ) : (
                          'Completed'
                        )}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1.5 empty:hidden">
                      {m.source_running_at && (
                        <StatusBadge
                          tone="danger"
                          icon={Power}
                          label="Source VM running"
                          title={`Running again since ${formatDateTime(m.source_running_at)}: its data diverges from RHOSO`}
                        />
                      )}
                      {isOverdue(m, plan.finalize_warning_h, now) && (
                        <StatusBadge tone="warning" icon={AlarmClock} label="Finalize overdue" title={`In Completed longer than the plan's ${plan.finalize_warning_h} h`} />
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </Panel>
  );
}

export interface FinalizePlanDialogProps {
  plan: Plan;
  migrations: Migration[];
  /** Open while set. */
  scope: FinalizeScope | null;
  onClose: () => void;
  /** The outcome: the page announces the counts and lists the VMs it skipped (SDD §16). */
  onDone: (result: PlanFinalizeResult) => void;
  /** Where focus goes on close when the Finalize button that opened the dialog goes with the finalize (SDD §16). */
  returnFocusRef?: RefObject<HTMLElement>;
}

/** The VMs a bulk finalize skipped, each with the reason the API gave, until dismissed (SDD §8.1, §16). */
export function FinalizeSkippedNotice({ skipped, onDismiss }: { skipped: SkippedVm[]; onDismiss: () => void }) {
  return (
    <section
      aria-label="Not finalized"
      className="flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-sm"
    >
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-warning" />
      <div className="min-w-0 flex-1">
        <p className="text-foreground">{skipped.length === 1 ? '1 VM was not finalized:' : `${skipped.length} VMs were not finalized:`}</p>
        <ul className="mt-1.5 flex flex-col gap-1">
          {skipped.map((s) => (
            <li key={s.id} className="break-words text-muted-foreground">
              <span className="font-mono text-foreground">{s.name}</span>: {s.reason}
            </li>
          ))}
        </ul>
      </div>
      <Button variant="ghost" size="sm" onClick={onDismiss}>
        Dismiss
      </Button>
    </section>
  );
}

/** Finalize a wave or the plan with one approval, typing its name (`POST /plans/{id}/finalize`, SDD §8.1). */
export function FinalizePlanDialog({ plan, migrations, scope, onClose, onDone, returnFocusRef }: FinalizePlanDialogProps) {
  const finalize = useFinalizePlan(plan.id);
  const [deleteSource, setDeleteSource] = useState(false);
  const deleteId = useId();
  const wave = scope?.wave ?? null;
  const name = wave ? wave.name : plan.name;
  const items = scope ? inScope(plan, migrations, wave) : [];
  const running = items.filter((m) => m.source_running_at);
  const one = items.length === 1;
  const close = () => {
    finalize.reset();
    setDeleteSource(false);
    onClose();
  };
  return (
    <ConfirmDialog
      open={scope !== null}
      tone="danger"
      title={`Finalize ${vms(items.length)} of ${name}?`}
      description={
        one
          ? 'Finalizing is irreversible: this VM can no longer be rolled back, and the temporary resources of its source VM are cleaned up.'
          : 'Finalizing is irreversible: these VMs can no longer be rolled back, and the temporary resources of their source VMs are cleaned up.'
      }
      confirmLabel={wave ? 'Finalize wave' : 'Finalize all'}
      requireText={name}
      returnFocusRef={returnFocusRef}
      pending={finalize.isPending}
      error={scope ? finalize.error : null}
      onConfirm={() =>
        finalize.mutate(
          { wave_id: wave?.id ?? null, confirm: name, delete_source: deleteSource },
          {
            onSuccess: (result) => {
              onDone(result);
              close();
            },
          },
        )
      }
      onCancel={close}
    >
      <ul aria-label="VMs to finalize" className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto">
        {items.map((m) => (
          <li key={m.id} className="rounded-sm border border-border px-1.5 py-0.5 font-mono text-xs text-foreground">
            {m.vm.name}
          </li>
        ))}
      </ul>
      {running.length > 0 && (
        <p className="flex items-start gap-2 rounded-md border border-status-danger/40 bg-status-danger/10 p-2.5 text-sm text-foreground">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-danger" />
          <span>
            {running.length === 1
              ? `The source VM of ${running[0]?.vm.name} runs again: what it wrote since is not on RHOSO. Stop it, or roll it back instead.`
              : `${running.length} of these source VMs run again: what they wrote since is not on RHOSO. Stop them, or roll them back instead.`}
          </span>
        </p>
      )}
      <label htmlFor={deleteId} className="flex min-h-11 cursor-pointer items-start gap-2.5 text-sm">
        <input id={deleteId} type="checkbox" className="mt-1 size-4 accent-accent" checked={deleteSource} onChange={(e) => setDeleteSource(e.target.checked)} />
        <span>
          {one ? 'Also delete the source VM' : 'Also delete the source VMs'}
          <span className="block text-xs text-muted-foreground">
            {one ? 'Leave unchecked to keep the stopped source VM for a while.' : 'Leave unchecked to keep the stopped source VMs for a while.'}
          </span>
        </span>
      </label>
    </ConfirmDialog>
  );
}
