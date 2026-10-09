import {
  ArrowRight,
  BadgeCheck,
  CircleSlash,
  RefreshCw,
  RotateCcw,
  ThumbsUp,
  Undo2,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { useId, useState } from 'react';
import { useMigrationAction, type MigrationActionRequest } from '../api/hooks';
import { ACTION_TEXT_MAX, type Migration, type Plan, type Role } from '../api/types';
import { cn } from '../lib/cn';
import { formatDateTime, formatDuration } from '../lib/format';
import { isWarmStrategy } from '../lib/phase';
import { MIGRATION_ACTION_ORDER, migrationActions, nextStep, waitsOnClosedWindow, type MigrationActionKey } from '../lib/migrationActions';
import { Button } from './Button';
import { ConfirmDialog } from './ConfirmDialog';
import { TextAreaField } from './Field';

const LABEL: Record<MigrationActionKey, string> = {
  approve: 'Approve',
  cutover: 'Cut over',
  sync: 'Sync now',
  rollback: 'Roll back',
  retry: 'Retry',
  cancel: 'Cancel',
  finalize: 'Finalize',
};

const ICON: Record<MigrationActionKey, LucideIcon> = {
  approve: ThumbsUp,
  cutover: Zap,
  sync: RefreshCw,
  rollback: Undo2,
  retry: RotateCcw,
  cancel: CircleSlash,
  finalize: BadgeCheck,
};

const DONE: Record<MigrationActionKey, string> = {
  approve: 'approved',
  cutover: 'cutover requested',
  sync: 'delta sync requested',
  rollback: 'rollback started',
  retry: 'retry requested',
  cancel: 'cancelled',
  finalize: 'finalized',
};

function windowOpen(plan: Plan | null | undefined): boolean {
  const w = plan?.cutover_window;
  if (!w) return true;
  const now = Date.now();
  return now >= Date.parse(w.start) && now <= Date.parse(w.end);
}

export interface MigrationActionsProps {
  migration: Migration;
  plan?: Plan | null;
  role: Role | undefined;
  /** The plan failed to load: its cutover window and approval policy are unknown (SDD §16). */
  planUnavailable?: boolean;
}

/**
 * Approve / Cut over / Sync / Retry / Roll back / Cancel / Finalize, enabled only for a valid phase
 * and sufficient role (unavailable ones stay focusable and say why). Every action confirms first;
 * destructive ones use the danger tone and Finalize requires typing the VM name.
 */
export function MigrationActions({ migration: m, plan, role, planUnavailable = false }: MigrationActionsProps) {
  const availability = migrationActions(m, role, plan);
  if (planUnavailable && availability.cutover.enabled) {
    availability.cutover = { enabled: false, reason: 'The plan could not be loaded: its cutover window and approval policy are unknown.' };
  }
  // a requested cutover waiting for a closed window: the Cutover action lets it start outside the window (SDD §5.4)
  const forcing = waitsOnClosedWindow(m, plan, Date.now());
  // a warm cutover requested before pre-copy converged starts once it does (SDD §5.4)
  const early = isWarmStrategy(m.strategy) && m.phase !== 'awaiting_cutover';
  const step = nextStep(m, plan);
  const action = useMigrationAction(m.id);
  const [open, setOpen] = useState<MigrationActionKey | null>(null);
  const [comment, setComment] = useState('');
  const [reason, setReason] = useState('');
  const [forceWindow, setForceWindow] = useState(false);
  const [deleteSource, setDeleteSource] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const forceId = useId();
  const deleteId = useId();

  const show = (key: MigrationActionKey) => {
    action.reset();
    setComment('');
    setReason('');
    setForceWindow(false);
    setDeleteSource(false);
    setOpen(key);
  };
  const close = () => {
    setOpen(null);
    action.reset();
  };
  const submit = (request: MigrationActionRequest) =>
    action.mutate(request, {
      onSuccess: () => {
        setAnnouncement(`${m.vm.name}: ${DONE[request.action]}.`);
        setOpen(null);
      },
    });

  const commentField = (
    <TextAreaField label="Comment (optional)" rows={2} maxLength={ACTION_TEXT_MAX} value={comment} onChange={(e) => setComment(e.target.value)} hint="Recorded in the audit trail." />
  );
  const estimate = m.estimate ? formatDuration(m.estimate.downtime_s) : 'unknown';
  const slo = plan ? formatDuration(plan.downtime_slo_s) : null;
  const closedWindow = !windowOpen(plan);
  const common = { pending: action.isPending, error: action.error, onCancel: close };

  return (
    <div className="flex flex-col gap-3">
      <p
        className={cn(
          'flex items-start gap-2 rounded-md border px-3 py-2 text-sm text-foreground',
          step.action ? 'border-accent/50 bg-accent/10' : 'border-border bg-muted/40',
        )}
      >
        <ArrowRight aria-hidden className={cn('mt-0.5 size-4 shrink-0', step.action ? 'text-status-success' : 'text-muted-foreground')} />
        {step.text}
      </p>
      <div role="group" aria-label="Migration actions" className="flex flex-wrap gap-2">
        {MIGRATION_ACTION_ORDER.map((key) => {
          const primary = key === step.action && availability[key].enabled;
          const destructive = key === 'rollback' || key === 'cancel' || key === 'finalize';
          return (
            <Button
              key={key}
              size="lg"
              icon={ICON[key]}
              variant={primary ? (destructive && key !== 'finalize' ? 'danger' : 'primary') : 'secondary'}
              disabledReason={availability[key].reason}
              onClick={() => show(key)}
              className={cn(destructive && key === 'cancel' && 'sm:ml-auto')}
            >
              {key === 'cutover' && forcing ? 'Cut over outside the window' : LABEL[key]}
            </Button>
          );
        })}
      </div>
      <p role="status" className="sr-only">
        {announcement}
      </p>

      <ConfirmDialog
        {...common}
        open={open === 'approve'}
        title={`Approve the cutover of ${m.vm.name}?`}
        description={`Records your approval. The cutover still waits for a cutover request${plan?.auto_cutover ? ' (automatic in this plan)' : ''} and the window.`}
        confirmLabel="Approve"
        onConfirm={() => submit({ action: 'approve', body: comment.trim() ? { comment: comment.trim() } : {} })}
      >
        {commentField}
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cutover'}
        title={
          forcing ? `Let ${m.vm.name} cut over outside the window?` : early ? `Cut over ${m.vm.name} once pre-copy converges?` : `Cut over ${m.vm.name} now?`
        }
        description={
          <>
            {forcing && plan?.cutover_window
              ? `The cutover was requested and waits for the cutover window (${formatDateTime(plan.cutover_window.start)} → ${formatDateTime(plan.cutover_window.end)}). Confirming lets it start as soon as the plan, its wave and a free cutover slot allow. `
              : ''}
            {early && !forcing
              ? 'The cutover is requested now and starts once pre-copy has converged and the plan, its wave and a free cutover slot allow. '
              : ''}
            {early && !forcing ? 'Then the' : 'The'} source VM will be stopped and the downtime clock starts. Estimated downtime {estimate}
            {slo ? ` against an SLO of ${slo}` : ''}. This also records your approval; the migration can be rolled back until it is finalized.
          </>
        }
        confirmLabel={forcing ? 'Cut over outside the window' : early ? 'Request cutover' : 'Start cutover'}
        onConfirm={() =>
          submit({
            action: 'cutover',
            body: { force_window: forcing || (closedWindow && forceWindow), ...(comment.trim() ? { comment: comment.trim() } : {}) },
          })
        }
      >
        {closedWindow && !forcing && plan?.cutover_window && (
          <label htmlFor={forceId} className="flex min-h-11 cursor-pointer items-start gap-2.5 rounded-md border border-status-warning/40 bg-status-warning/10 p-2 text-sm">
            <input id={forceId} type="checkbox" className="mt-1 size-4 accent-accent" checked={forceWindow} onChange={(e) => setForceWindow(e.target.checked)} />
            <span>
              Ignore the cutover window
              <span className="block text-xs text-muted-foreground">
                The window is {formatDateTime(plan.cutover_window.start)} → {formatDateTime(plan.cutover_window.end)}. Without this the cutover waits for it.
              </span>
            </span>
          </label>
        )}
        {commentField}
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'sync'}
        title={`Run a delta sync for ${m.vm.name}?`}
        description="Starts a keep-warm pass now so the final delta at cutover stays small. The source VM keeps running."
        confirmLabel="Sync now"
        onConfirm={() => submit({ action: 'sync' })}
      />

      <ConfirmDialog
        {...common}
        open={open === 'retry'}
        title={`Retry ${m.vm.name}?`}
        description={`The migration returns to ready and starts over from its first step.${
          m.downtime_started_at && !m.downtime_ended_at
            ? ' The source VM is still stopped: its downtime clock keeps running until the next cutover is verified.'
            : ''
        } Make sure the cause of the failure is fixed.`}
        confirmLabel="Retry"
        onConfirm={() => submit({ action: 'retry' })}
      />

      <ConfirmDialog
        {...common}
        open={open === 'rollback'}
        tone="danger"
        title={`Roll back ${m.vm.name}?`}
        description="Deletes the RHOSO instance (volumes are kept) and starts the source VM again. Downtime ends when the source is running."
        confirmLabel="Roll back"
        canConfirm={reason.trim().length > 0}
        confirmReason="Enter a reason first."
        onConfirm={() => submit({ action: 'rollback', body: { reason: reason.trim() } })}
      >
        <TextAreaField label="Reason" required rows={2} maxLength={ACTION_TEXT_MAX} value={reason} onChange={(e) => setReason(e.target.value)} hint="Required; recorded in the audit trail." />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'cancel'}
        tone="danger"
        title={`Cancel ${m.vm.name}?`}
        description="The migration stops for good and is removed from its wave. The source VM is not touched."
        confirmLabel="Cancel migration"
        cancelLabel="Keep migration"
        onConfirm={() => submit({ action: 'cancel', body: reason.trim() ? { reason: reason.trim() } : {} })}
      >
        <TextAreaField label="Reason (optional)" rows={2} maxLength={ACTION_TEXT_MAX} value={reason} onChange={(e) => setReason(e.target.value)} />
      </ConfirmDialog>

      <ConfirmDialog
        {...common}
        open={open === 'finalize'}
        tone="danger"
        title={`Finalize ${m.vm.name}?`}
        description="Finalizing is irreversible: rollback is no longer possible and the source VM's temporary resources are cleaned up."
        confirmLabel="Finalize"
        requireText={m.vm.name}
        onConfirm={() => submit({ action: 'finalize', body: { confirm: m.vm.name, delete_source: deleteSource } })}
      >
        <label htmlFor={deleteId} className="flex min-h-11 cursor-pointer items-start gap-2.5 text-sm">
          <input id={deleteId} type="checkbox" className="mt-1 size-4 accent-accent" checked={deleteSource} onChange={(e) => setDeleteSource(e.target.checked)} />
          <span>
            Also delete the source VM
            <span className="block text-xs text-muted-foreground">Leave unchecked to keep the stopped source VM for a while.</span>
          </span>
        </label>
      </ConfirmDialog>
    </div>
  );
}
